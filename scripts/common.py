"""Arena Parimutuel — primitive condivise.

Questo modulo e' l'unico posto in cui vivono:

* la forma canonica dei dati (§3 della spec),
* la catena di hash del ledger (I1),
* il reducer dei saldi (I2),
* la stringa canonica firmata e la verifica HMAC (I10),
* la pipeline di validazione/applicazione di una bet (§8).

Tutti gli script (`place_bet`, `drain`, `settle`, `credit`, `register`,
`create_event`, `rebuild_balances`) passano da qui: cosi' il canale d'ingresso
puo' cambiare senza toccare le regole del denaro.

Solo stdlib. Nessun float nei percorsi di denaro: tutto in interi (minor unit).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------
# Costanti
# --------------------------------------------------------------------------

SCHEMA_VERSION = 1
GENESIS_HASH = "0" * 64
SIG_PREFIX = "v1"
SIDES = ("yes", "no")
OUTCOMES = ("yes", "no", "void")

KIND_CREDIT = "CREDIT"
KIND_BET_DEBIT = "BET_DEBIT"
KIND_SETTLE_USER = "SETTLE_USER"
KINDS = (KIND_CREDIT, KIND_BET_DEBIT, KIND_SETTLE_USER)

# Codici ricevuta (§8.2)
OK_APPLIED = "OK_APPLIED"
DUP = "DUP"
ERR_MALFORMED = "ERR_MALFORMED"
ERR_UNKNOWN_USER = "ERR_UNKNOWN_USER"
ERR_SIG = "ERR_SIG"
ERR_EVENT_MISSING = "ERR_EVENT_MISSING"
ERR_EVENT_CLOSED = "ERR_EVENT_CLOSED"
ERR_LIMITS = "ERR_LIMITS"
ERR_NONCE = "ERR_NONCE"
ERR_BALANCE = "ERR_BALANCE"

SUCCESS_CODES = (OK_APPLIED, DUP)

# Codici di uscita degli script. La distinzione che conta per un sistema
# contabile: una bet RIFIUTATA e' un esito normale (la ricevuta e' scritta,
# l'infrastruttura ha funzionato), un errore di infrastruttura NO. Mescolare i
# due dietro un `|| true` nasconde i bug veri.
EXIT_OK = 0          # lavoro svolto (applicata, duplicata, coda vuota)
EXIT_ERROR = 1       # errore di infrastruttura: git, filesystem, rete, bug
EXIT_USAGE = 2       # invocazione o configurazione sbagliata
EXIT_REJECTED = 10   # bet rifiutata dalla validazione: NON e' un guasto
EXIT_CONFLICT = 11   # push rifiutato: un altro scrittore e' passato davanti

BET_FIELDS = ("bet_id", "user_id", "event_id", "side", "amount", "nonce", "ts", "sig")
STRING_BET_FIELDS = ("bet_id", "user_id", "event_id", "side", "ts", "sig")

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
USER_ID_RE = re.compile(r"^u_[0-9a-f]{8}$")


# --------------------------------------------------------------------------
# Percorsi (risolti a ogni chiamata: i test spostano ARENA_ROOT)
# --------------------------------------------------------------------------

def root() -> Path:
    env = os.environ.get("ARENA_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def events_path() -> Path:
    return root() / "events.json"


def balances_path() -> Path:
    return root() / "balances.json"


def settled_path() -> Path:
    return root() / "settled.json"


def receipts_path() -> Path:
    return root() / "receipts.json"


def ledger_dir() -> Path:
    return root() / "ledger"


def ledger_state_path() -> Path:
    return ledger_dir() / "state.json"


def secrets_age_path() -> Path:
    return root() / "secrets.age"


# --------------------------------------------------------------------------
# Tempo
# --------------------------------------------------------------------------

def now_iso() -> str:
    """Ora corrente ISO-8601 UTC, al secondo: YYYY-MM-DDTHH:MM:SSZ."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)


def parse_iso(value: str) -> datetime:
    """Parsa un timestamp ISO-8601; accetta sia `...Z` sia `...+00:00`."""
    if not isinstance(value, str):
        raise ValueError("timestamp non stringa")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def shard_for(ts: str) -> str:
    """Nome dello shard mensile del ledger per un timestamp."""
    return parse_iso(ts).strftime("%Y-%m") + ".ndjson"


# --------------------------------------------------------------------------
# Hashing / canonicalizzazione
# --------------------------------------------------------------------------

def canonical(obj) -> str:
    """Serializzazione deterministica usata per hash e firma."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def entry_hash(entry: dict) -> str:
    """hash = SHA256(canonical(entry senza "hash"))  (I1)."""
    payload = {k: v for k, v in entry.items() if k != "hash"}
    return sha256_hex(canonical(payload))


# --------------------------------------------------------------------------
# Firma delle bet (§3.6, I10)
# --------------------------------------------------------------------------

def _sig_field(value) -> str:
    """Rende un campo nella stringa canonica senza sorprese di formato."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def bet_signing_string(bet: dict) -> str:
    """Stringa canonica firmata; ordine FISSO, mai derivato dal JSON."""
    return "|".join(
        [
            SIG_PREFIX,
            _sig_field(bet.get("bet_id")),
            _sig_field(bet.get("user_id")),
            _sig_field(bet.get("event_id")),
            _sig_field(bet.get("side")),
            _sig_field(bet.get("amount")),
            _sig_field(bet.get("nonce")),
            _sig_field(bet.get("ts")),
        ]
    )


def sign_bet(secret: str, bet: dict) -> str:
    return hmac.new(
        secret.encode("utf-8"), bet_signing_string(bet).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def verify_bet_sig(secret: str, bet: dict) -> bool:
    """Confronto a tempo costante fra sig attesa e sig ricevuta."""
    got = bet.get("sig")
    if not isinstance(got, str):
        return False
    return hmac.compare_digest(sign_bet(secret, bet), got.strip().lower())


# --------------------------------------------------------------------------
# I/O JSON (scrittura atomica: tmp + replace nella stessa directory)
# --------------------------------------------------------------------------

def load_json(path: Path, default=None):
    path = Path(path)
    if not path.exists():
        return json.loads(json.dumps(default)) if default is not None else None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, obj) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------
# Documenti di default (§3)
# --------------------------------------------------------------------------

def default_events() -> dict:
    return {"version": SCHEMA_VERSION, "events": {}}


def default_balances() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "as_of_seq": 0,
        "head_hash": GENESIS_HASH,
        "updated_at": now_iso(),
        "balances": {},
    }


def default_settled() -> dict:
    return {"version": SCHEMA_VERSION, "settlements": {}}


def default_receipts() -> dict:
    return {"version": SCHEMA_VERSION, "receipts": {}}


def default_ledger_state() -> dict:
    return {
        "version": SCHEMA_VERSION,
        "last_seq": 0,
        "head_hash": GENESIS_HASH,
        "applied_bets": {},
        "current_shard": None,
    }


def zero_balance() -> dict:
    return {"available": 0, "at_risk": 0, "lifetime_credited": 0, "last_nonce": 0}


# --------------------------------------------------------------------------
# Lettura del ledger
# --------------------------------------------------------------------------

def ledger_shards(base: Path = None) -> list:
    """Shard mensili in ordine cronologico (il nome YYYY-MM ordina da solo)."""
    base = Path(base) if base is not None else ledger_dir()
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.suffix == ".ndjson")


def iter_ledger(base: Path = None):
    """Itera le entry del ledger, shard per shard, in ordine di scrittura."""
    for shard in ledger_shards(base):
        with shard.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{shard.name}:{lineno}: riga non JSON ({exc})")


def read_ledger(base: Path = None) -> list:
    return list(iter_ledger(base))


def find_entry_by_id(entry_id: str, base: Path = None):
    """Ricerca lineare per `id`: serve all'idempotenza di credit/settle."""
    for entry in iter_ledger(base):
        if entry.get("id") == entry_id:
            return entry
    return None


def verify_chain(entries) -> list:
    """Verifica I1: seq contigui, prev_hash concatenati, hash ricomputabili."""
    problems = []
    prev_hash = GENESIS_HASH
    expected_seq = 1
    for entry in entries:
        where = f"seq={entry.get('seq')} id={entry.get('id')}"
        if entry.get("seq") != expected_seq:
            problems.append(f"{where}: seq atteso {expected_seq}")
        if entry.get("prev_hash") != prev_hash:
            problems.append(f"{where}: prev_hash rotto (atteso {prev_hash})")
        recomputed = entry_hash(entry)
        if entry.get("hash") != recomputed:
            problems.append(f"{where}: hash non verifica (atteso {recomputed})")
        if entry.get("kind") not in KINDS:
            problems.append(f"{where}: kind sconosciuto {entry.get('kind')!r}")
        prev_hash = entry.get("hash")
        expected_seq = (entry.get("seq") or expected_seq) + 1
    return problems


# --------------------------------------------------------------------------
# Reducer dei saldi (I2) — saldo = funzione pura del ledger
# --------------------------------------------------------------------------

def reduce_balances(entries):
    """Ritorna (balances, last_seq, head_hash) ricostruiti dalle sole entry."""
    balances = {}
    last_seq = 0
    head = GENESIS_HASH

    def row(user_id):
        return balances.setdefault(user_id, zero_balance())

    for entry in entries:
        kind = entry.get("kind")
        user_id = entry.get("user_id")
        amount = entry.get("amount", 0)
        rec = row(user_id)
        if kind == KIND_CREDIT:
            rec["available"] += amount
            if amount > 0:
                rec["lifetime_credited"] += amount
        elif kind == KIND_BET_DEBIT:
            rec["available"] -= amount
            rec["at_risk"] += amount
            rec["last_nonce"] = entry.get("nonce") or rec["last_nonce"]
        elif kind == KIND_SETTLE_USER:
            rec["available"] += amount
            rec["at_risk"] -= int((entry.get("meta") or {}).get("stake", 0))
        else:
            raise ValueError(f"kind sconosciuto: {kind!r} (seq={entry.get('seq')})")
        last_seq = entry.get("seq", last_seq)
        head = entry.get("hash", head)

    return balances, last_seq, head


def normalize_balances(balances: dict) -> dict:
    """Toglie le righe interamente a zero.

    Una riga a zero non porta informazione (la crea `register` per comodita'
    di UI) quindi non e' un disallineamento rispetto al ledger.
    """
    zero = zero_balance()
    return {u: dict(r) for u, r in balances.items() if dict(r) != zero}


def diff_balances(projected: dict, rebuilt: dict) -> list:
    """Differenze riga per riga fra proiezione e ricostruzione (I2)."""
    left = normalize_balances(projected)
    right = normalize_balances(rebuilt)
    problems = []
    for user_id in sorted(set(left) | set(right)):
        a = left.get(user_id)
        b = right.get(user_id)
        if a is None:
            problems.append(f"{user_id}: assente nella proiezione, ledger dice {b}")
        elif b is None:
            problems.append(f"{user_id}: presente in proiezione ({a}) ma non nel ledger")
        elif a != b:
            for field in sorted(set(a) | set(b)):
                if a.get(field) != b.get(field):
                    problems.append(
                        f"{user_id}.{field}: proiezione={a.get(field)} ledger={b.get(field)}"
                    )
    return problems


# --------------------------------------------------------------------------
# Magazzino segreti (§5.2)
# --------------------------------------------------------------------------

class SecretsError(RuntimeError):
    pass


def secrets_backend() -> str:
    """Backend attivo: 'file' (dev/test), 'age' (scala), 'env' (piccolo), 'none'."""
    if os.environ.get("ARENA_SECRETS_FILE"):
        return "file"
    if os.environ.get("AGE_KEY") and secrets_age_path().exists():
        return "age"
    if os.environ.get("USER_SECRETS"):
        return "env"
    if os.environ.get("AGE_KEY"):
        return "age"
    return "none"


def _age_identity_file(stack: list) -> str:
    key = os.environ.get("AGE_KEY", "").strip()
    if not key:
        raise SecretsError("AGE_KEY non impostata")
    fd, path = tempfile.mkstemp(prefix="age-", suffix=".key")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key + "\n")
    stack.append(path)
    return path


def _run(cmd, **kwargs):
    try:
        return subprocess.run(cmd, check=True, capture_output=True, **kwargs)
    except FileNotFoundError as exc:
        raise SecretsError(f"binario mancante: {cmd[0]} ({exc})")
    except subprocess.CalledProcessError as exc:
        err = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise SecretsError(f"{' '.join(cmd)} fallito: {err}")


def _parse_secrets_blob(text: str) -> dict:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise SecretsError("il magazzino segreti non e' un oggetto JSON")
    out = {}
    for user_id, secret in data.items():
        if not isinstance(secret, str):
            raise SecretsError(f"segreto non stringa per {user_id}")
        out[user_id] = secret
    return out


def load_user_secrets() -> dict:
    """Mappa user_id -> secret (in chiaro, solo in memoria)."""
    backend = secrets_backend()
    if backend == "file":
        path = Path(os.environ["ARENA_SECRETS_FILE"])
        if not path.exists():
            return {}
        return _parse_secrets_blob(path.read_text(encoding="utf-8"))
    if backend == "env":
        return _parse_secrets_blob(os.environ["USER_SECRETS"])
    if backend == "age":
        blob = secrets_age_path()
        if not blob.exists():
            return {}
        tmps = []
        try:
            ident = _age_identity_file(tmps)
            proc = _run(["age", "--decrypt", "--identity", ident, str(blob)])
            return _parse_secrets_blob(proc.stdout.decode("utf-8"))
        finally:
            for path in tmps:
                if os.path.exists(path):
                    os.unlink(path)
    raise SecretsError(
        "nessun magazzino segreti configurato: imposta USER_SECRETS, AGE_KEY "
        "(con secrets.age) oppure ARENA_SECRETS_FILE"
    )


def save_user_secrets(secrets: dict) -> str:
    """Riscrive il magazzino. Ritorna il backend usato.

    `USER_SECRETS` (Actions secret) non e' scrivibile da un workflow: in quel
    caso la registrazione resta semi-manuale (§5.2) e questa funzione alza.
    """
    backend = secrets_backend()
    text = json.dumps(secrets, indent=2, sort_keys=True) + "\n"
    if backend == "file":
        path = Path(os.environ["ARENA_SECRETS_FILE"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return "file"
    if backend == "age":
        tmps = []
        try:
            ident = _age_identity_file(tmps)
            recipient = _run(["age-keygen", "-y", ident]).stdout.decode().strip()
            if not recipient:
                raise SecretsError("impossibile derivare il recipient da AGE_KEY")
            out = _run(
                ["age", "--encrypt", "--armor", "--recipient", recipient],
                input=text.encode("utf-8"),
            ).stdout
            secrets_age_path().write_bytes(out)
            return "age"
        finally:
            for path in tmps:
                if os.path.exists(path):
                    os.unlink(path)
    raise SecretsError(
        "il magazzino segreti attivo non e' scrivibile da qui "
        f"(backend={backend}): aggiungi il segreto a mano all'Actions secret USER_SECRETS"
    )


# --------------------------------------------------------------------------
# Stato di lavoro: una copia in memoria, un solo flush, un solo commit (I7)
# --------------------------------------------------------------------------

class WorkingState:
    """Copia di lavoro di tutto lo stato mutabile.

    Le mutazioni avvengono in memoria (cosi' un lotto di bet vede i saldi
    aggiornati bet dopo bet, §7.3) e vengono materializzate una sola volta da
    `flush()`; il commit git che segue e' il confine atomico.
    """

    def __init__(self, base: Path = None):
        self.root = Path(base).resolve() if base is not None else root()
        self.state = load_json(self._p("ledger/state.json"), default_ledger_state())
        self.balances_doc = load_json(self._p("balances.json"), default_balances())
        self.events_doc = load_json(self._p("events.json"), default_events())
        self.receipts_doc = load_json(self._p("receipts.json"), default_receipts())
        self.settled_doc = load_json(self._p("settled.json"), default_settled())
        self.pending = []          # [(shard, entry)] non ancora su disco
        self.touched = set()       # file da includere nel commit

    # -- helper ------------------------------------------------------------
    def _p(self, rel: str) -> Path:
        return self.root / rel

    @property
    def balances(self) -> dict:
        return self.balances_doc.setdefault("balances", {})

    @property
    def events(self) -> dict:
        return self.events_doc.setdefault("events", {})

    @property
    def receipts(self) -> dict:
        return self.receipts_doc.setdefault("receipts", {})

    @property
    def settlements(self) -> dict:
        return self.settled_doc.setdefault("settlements", {})

    @property
    def applied_bets(self) -> dict:
        return self.state.setdefault("applied_bets", {})

    def balance(self, user_id: str) -> dict:
        """Riga saldo dell'utente, creata a zero se assente."""
        return self.balances.setdefault(user_id, zero_balance())

    # -- ledger ------------------------------------------------------------
    def append_entry(
        self,
        kind: str,
        entry_id: str,
        user_id: str,
        amount: int,
        event_id=None,
        side=None,
        nonce=None,
        meta=None,
        ts: str = None,
    ) -> dict:
        """Aggiunge una entry alla catena (I1). Non scrive su disco."""
        if kind not in KINDS:
            raise ValueError(f"kind non valido: {kind!r}")
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise ValueError(f"amount deve essere intero, non {type(amount).__name__}")
        ts = ts or now_iso()
        entry = {
            "seq": int(self.state["last_seq"]) + 1,
            "ts": ts,
            "kind": kind,
            "id": entry_id,
            "user_id": user_id,
            "event_id": event_id,
            "side": side,
            "amount": amount,
            "nonce": nonce,
            "prev_hash": self.state["head_hash"],
        }
        if meta:
            entry["meta"] = meta
        entry["hash"] = entry_hash(entry)

        shard = shard_for(ts)
        self.state["last_seq"] = entry["seq"]
        self.state["head_hash"] = entry["hash"]
        self.state["current_shard"] = shard
        self.pending.append((shard, entry))
        return entry

    # -- persistenza -------------------------------------------------------
    def flush(self) -> list:
        """Materializza tutto su disco. Ritorna i path toccati (relativi)."""
        written = set(self.touched)

        by_shard = {}
        for shard, entry in self.pending:
            by_shard.setdefault(shard, []).append(entry)
        for shard, entries in by_shard.items():
            path = self._p("ledger") / shard
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(canonical(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            written.add(f"ledger/{shard}")
        self.pending = []

        self.balances_doc["version"] = SCHEMA_VERSION
        self.balances_doc["as_of_seq"] = self.state["last_seq"]
        self.balances_doc["head_hash"] = self.state["head_hash"]
        self.balances_doc["updated_at"] = now_iso()

        save_json(self._p("ledger/state.json"), self.state)
        save_json(self._p("balances.json"), self.balances_doc)
        save_json(self._p("events.json"), self.events_doc)
        save_json(self._p("receipts.json"), self.receipts_doc)
        save_json(self._p("settled.json"), self.settled_doc)
        written.update(
            {
                "ledger/state.json",
                "balances.json",
                "events.json",
                "receipts.json",
                "settled.json",
            }
        )
        self.touched = set()
        return sorted(written)


# --------------------------------------------------------------------------
# Ricevute (§3.7)
# --------------------------------------------------------------------------

def make_receipt(code: str, seq=None, available=None, reason=None, ts: str = None) -> dict:
    receipt = {
        "status": "APPLIED" if code in SUCCESS_CODES else "REJECTED",
        "code": code,
        "ts": ts or now_iso(),
    }
    if seq is not None:
        receipt["seq"] = seq
    if available is not None:
        receipt["available"] = available
    if reason:
        receipt["reason"] = reason
    return receipt


class BetResult:
    """Esito dell'ingestione di una singola bet."""

    def __init__(self, bet_id, code, receipt, entry=None):
        self.bet_id = bet_id
        self.code = code
        self.receipt = receipt
        self.entry = entry

    @property
    def ok(self) -> bool:
        return self.code in SUCCESS_CODES

    @property
    def applied(self) -> bool:
        """True solo se questa chiamata ha scritto una BET_DEBIT."""
        return self.code == OK_APPLIED

    def summary(self) -> str:
        bits = [f"{self.bet_id or '<no bet_id>'}: {self.receipt['status']} {self.code}"]
        if self.receipt.get("reason"):
            bits.append(f"({self.receipt['reason']})")
        if self.receipt.get("seq") is not None:
            bits.append(f"seq={self.receipt['seq']}")
        if self.receipt.get("available") is not None:
            bits.append(f"available={self.receipt['available']}")
        return " ".join(bits)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------
# Pipeline di validazione + applicazione (§8) — condivisa da place_bet e drain
# --------------------------------------------------------------------------

def validate_and_apply(bet, ws: WorkingState, secrets: dict, now: str = None) -> BetResult:
    """Valida una bet nell'ordine esatto di §8.1 e, se passa, la applica.

    Muta `ws` in memoria: il chiamante decide quando fare flush+commit.
    La ricevuta viene sempre registrata in `ws.receipts` (anche per i rifiuti)
    perche' il client fa polling su `receipts.json`, non sulle issue (§7.4).
    """
    ts = now or now_iso()

    def finish(code, reason=None, seq=None, available=None, entry=None, bet_id=None):
        bid = bet_id if bet_id is not None else (bet or {}).get("bet_id")
        receipt = make_receipt(code, seq=seq, available=available, reason=reason, ts=ts)
        if isinstance(bid, str) and bid:
            ws.receipts[bid] = receipt
        return BetResult(bid if isinstance(bid, str) else None, code, receipt, entry)

    # 1. schema / parse
    if not isinstance(bet, dict):
        return finish(ERR_MALFORMED, "payload non e' un oggetto JSON", bet_id=None)
    missing = [f for f in BET_FIELDS if bet.get(f) is None]
    if missing:
        return finish(ERR_MALFORMED, "campi mancanti: " + ",".join(missing))
    bad_types = [f for f in STRING_BET_FIELDS if not isinstance(bet.get(f), str)]
    if bad_types:
        return finish(ERR_MALFORMED, "campi non stringa: " + ",".join(bad_types))

    bet_id = bet["bet_id"]
    user_id = bet["user_id"]

    # 2. idempotenza (I6) — prima di tutto il resto: un replay non e' un errore
    if bet_id in ws.applied_bets:
        seq = ws.applied_bets[bet_id]
        available = ws.balances.get(user_id, zero_balance())["available"]
        return finish(DUP, "bet gia' applicata", seq=seq, available=available)

    # 3. utente noto
    secret = secrets.get(user_id)
    if not secret:
        return finish(ERR_UNKNOWN_USER, f"user_id sconosciuto: {user_id}")

    # 4. HMAC (I10)
    if not verify_bet_sig(secret, bet):
        return finish(ERR_SIG, "firma non valida")

    # 5. tipi e limiti sintattici
    amount = bet["amount"]
    nonce = bet["nonce"]
    if not _is_int(amount) or not _is_int(nonce):
        return finish(ERR_MALFORMED, "amount e nonce devono essere interi")
    if amount <= 0:
        return finish(ERR_MALFORMED, "amount deve essere > 0")
    if nonce <= 0:
        return finish(ERR_MALFORMED, "nonce deve essere > 0")
    if bet["side"] not in SIDES:
        return finish(ERR_MALFORMED, "side deve essere 'yes' o 'no'")
    if bet_id != f"{user_id}-{nonce}":
        return finish(ERR_MALFORMED, "bet_id deve essere '<user_id>-<nonce>'")
    try:
        parse_iso(bet["ts"])
    except ValueError:
        return finish(ERR_MALFORMED, "ts non e' un ISO-8601 valido")

    # 6. evento esistente
    event = ws.events.get(bet["event_id"])
    if event is None:
        return finish(ERR_EVENT_MISSING, f"evento sconosciuto: {bet['event_id']}")

    # 7. evento aperto e dentro la finestra (§6: freeze a close_at)
    if event.get("state") != "OPEN":
        return finish(ERR_EVENT_CLOSED, f"evento in stato {event.get('state')}")
    try:
        closed = parse_iso(ts) >= parse_iso(event["close_at"])
    except (KeyError, ValueError):
        return finish(ERR_EVENT_CLOSED, "close_at dell'evento non valido")
    if closed:
        return finish(ERR_EVENT_CLOSED, f"finestra chiusa a {event['close_at']}")

    # 8. limiti di puntata
    min_bet = int(event.get("min_bet", 1))
    max_bet = int(event.get("max_bet", 2 ** 62))
    if amount < min_bet or amount > max_bet:
        return finish(ERR_LIMITS, f"amount fuori dai limiti [{min_bet},{max_bet}]")

    # 9. nonce strettamente crescente (I5)
    row = ws.balance(user_id)
    if nonce <= int(row["last_nonce"]):
        return finish(ERR_NONCE, f"nonce {nonce} <= last_nonce {row['last_nonce']}")

    # 10. saldo (I4)
    if amount > int(row["available"]):
        return finish(ERR_BALANCE, f"available {row['available']} < amount {amount}")

    # 11. applica (§8.3)
    entry = ws.append_entry(
        KIND_BET_DEBIT,
        entry_id=bet_id,
        user_id=user_id,
        amount=amount,
        event_id=bet["event_id"],
        side=bet["side"],
        nonce=nonce,
        ts=ts,
        meta={"bet_ts": bet["ts"]},
    )
    ws.applied_bets[bet_id] = entry["seq"]
    row["available"] -= amount
    row["at_risk"] += amount
    row["last_nonce"] = nonce
    pool = event.setdefault("pool", {"yes": 0, "no": 0})
    pool[bet["side"]] = int(pool.get(bet["side"], 0)) + amount
    counts = event.setdefault("bet_count", {"yes": 0, "no": 0})
    counts[bet["side"]] = int(counts.get(bet["side"], 0)) + 1

    return finish(
        OK_APPLIED, seq=entry["seq"], available=row["available"], entry=entry
    )


# --------------------------------------------------------------------------
# Quote (usate da client e UI) — unico punto con i float, mai nel denaro
# --------------------------------------------------------------------------

def implied_multiplier(pool: dict, side: str, takeout_bps: int):
    """Moltiplicatore atteso per 1 unita' puntata su `side`; None se pool 0."""
    s = int(pool.get(side, 0))
    total = int(pool.get("yes", 0)) + int(pool.get("no", 0))
    if s <= 0 or total <= 0:
        return None
    return (1.0 - takeout_bps / 10000.0) * total / s


# --------------------------------------------------------------------------
# Git (il commit del run e' il confine atomico, §7.4)
# --------------------------------------------------------------------------

class PushRejected(RuntimeError):
    """Il remoto e' avanzato: il lavoro va rifatto su un checkout aggiornato.

    Non e' un guasto e non si risolve forzando: il commit locale descrive uno
    stato che non esiste piu'. Si riparte da `origin` e si ricalcola tutto.
    """


def git(*args, cwd: Path = None, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or root()),
        check=check,
        capture_output=True,
        text=True,
    )


def commit_and_push(paths, message: str, cwd: Path = None, push: bool = True) -> bool:
    """Un run = un commit. Ritorna False se non c'era nulla da committare.

    Il push viene ritentato solo su errori di rete (2s, 4s, 8s, 16s): un
    rifiuto non-fast-forward significa che un altro scrittore e' passato
    davanti e va risolto rileggendo lo stato, non forzando.
    """
    cwd = cwd or root()
    rel = [str(p) for p in paths]
    if not rel:
        return False
    git("add", "--", *rel, cwd=cwd)
    status = git("status", "--porcelain", "--", *rel, cwd=cwd).stdout.strip()
    if not status:
        return False

    env_name = os.environ.get("GIT_AUTHOR_NAME")
    if not env_name:
        git("config", "user.name", "arena-bot", cwd=cwd)
        git("config", "user.email", "arena-bot@users.noreply.github.com", cwd=cwd)
    git("commit", "-m", message, cwd=cwd)
    if not push:
        return True

    # Hook DI SOLO TEST: allarga la finestra fra commit e push per rendere
    # deterministica la corsa fra scrittori. In produzione la variabile non
    # esiste e questa riga non fa nulla.
    test_delay = float(os.environ.get("ARENA_TEST_PUSH_DELAY", "0") or 0)
    if test_delay:
        time.sleep(test_delay)

    branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()
    delay = 2
    last = None
    for attempt in range(5):
        proc = git("push", "-u", "origin", branch, cwd=cwd, check=False)
        if proc.returncode == 0:
            return True
        last = (proc.stdout or "") + (proc.stderr or "")
        if "non-fast-forward" in last or "rejected" in last or "fetch first" in last:
            raise PushRejected(
                "push rifiutato (un altro scrittore e' passato davanti): "
                "si rifa' il lavoro su un checkout aggiornato, non si forza.\n"
                + last
            )
        if attempt == 4:
            break
        sys.stderr.write(f"push fallito, riprovo fra {delay}s...\n")
        time.sleep(delay)
        delay *= 2
    raise RuntimeError(f"push fallito dopo 5 tentativi:\n{last}")


def commit_and_push_cli(paths, message: str, push: bool = True) -> int:
    """`commit_and_push` per gli script: traduce i guasti in codici di uscita.

    Nessuno script deve trattare un conflitto di push come "fatto": il lavoro
    non e' pubblicato, quindi per gli altri non e' mai successo.
    """
    try:
        commit_and_push(paths, message, push=push)
        return EXIT_OK
    except PushRejected as exc:
        eprint(f"CONFLITTO: {exc}")
        return EXIT_CONFLICT
    except (RuntimeError, OSError) as exc:
        eprint(f"errore di infrastruttura durante il commit: {exc}")
        return EXIT_ERROR


def reset_to_remote(cwd: Path = None, branch: str = None) -> str:
    """Riporta la working tree esattamente a `origin/<branch>`.

    E' l'equivalente in-process del checkout pulito con cui GitHub Actions fa
    partire ogni run: si usa SOLO dopo un push rifiutato, quando il commit
    locale e' certamente non pubblicato e va buttato.
    """
    cwd = cwd or root()
    branch = branch or git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd).stdout.strip()

    # Il fetch passa dalla rete e puo' fallire per motivi transitori (hiccup,
    # contesa sui ref lato server). Qui i tempi sono corti perche' siamo gia'
    # dentro un run che ha perso una corsa: se non si recupera in fretta tanto
    # vale lasciare il lavoro al prossimo run.
    delay = 0.5
    for attempt in range(4):
        proc = git("fetch", "origin", branch, cwd=cwd, check=False)
        if proc.returncode == 0:
            break
        if attempt == 3:
            raise RuntimeError(
                f"fetch di origin/{branch} fallito dopo 4 tentativi: "
                + ((proc.stderr or proc.stdout or "").strip() or "senza messaggio")
            )
        time.sleep(delay)
        delay *= 2
    git("reset", "--hard", f"origin/{branch}", cwd=cwd)
    git("clean", "-fd", "--", "ledger", cwd=cwd, check=False)
    return git("rev-parse", "HEAD", cwd=cwd).stdout.strip()


def clean_id(value: str) -> str:
    """Normalizza un identificatore digitato da un umano.

    Serve agli argomenti degli script dell'owner (`--event`, `--user`, `--id`):
    da telefono la tastiera aggiunge spazi con facilita' e un `ev_prova ` che
    non trova l'evento e' solo frustrazione.

    NON si usa sul payload di una bet: li' la stringa esatta e' quella che
    l'HMAC copre, e normalizzarla dopo la verifica significherebbe applicare
    una bet diversa da quella firmata.
    """
    return (value or "").strip()


def eprint(*args):
    print(*args, file=sys.stderr)
