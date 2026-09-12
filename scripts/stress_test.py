#!/usr/bin/env python3
"""Stress test di concorrenza: molti scrittori contro lo stesso repo.

Simula il modello di GitHub Actions **senza** la serializzazione della
`concurrency`: piu' run partono davvero insieme, ognuno da un proprio clone, e
si contendono il push verso un bare repo. E' il caso peggiore — su GitHub il
gruppo `ledger-write` li mette in fila — e serve a dimostrare che le invarianti
reggono anche quando la serializzazione non c'e'.

    python3 scripts/stress_test.py                          # profilo veloce
    python3 scripts/stress_test.py --users 50 --bets 6 --runners 8 --crash-rate 0.3
    python3 scripts/stress_test.py --keep                    # non cancella l'arena

Cosa mette sotto pressione:

* N utenti che puntano in parallelo su piu' eventi;
* issue duplicate (stessa bet consegnata piu' volte);
* bet ostili: firma forgiata, utente sconosciuto, saldo insufficiente,
  evento chiuso, corpo illeggibile, nonce non monotono;
* crash veri (SIGKILL) in punti casuali del drain, incluso fra il push e la
  chiusura delle issue;
* piu' settlement simultanei dello stesso evento.

Alla fine ricontrolla tutto da un clone pulito: catena, saldi, nonce, pool,
idempotenza e conservazione globale del denaro.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STATE_FILES = ("events.json", "balances.json", "settled.json", "receipts.json")


def iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime(c.ISO_FMT)


def git(*args, cwd: Path, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), check=check,
                          capture_output=True, text=True)


def log(msg: str):
    print(msg, flush=True)


def git_retry(*args, cwd: Path, attempts: int = 5):
    """git con ritentativi: 12 processi sullo stesso bare repo locale si
    contendono i lock dei ref e un fetch puo' fallire per un istante. E' un
    limite del banco di prova (il server GitHub serializza da solo), non del
    sistema sotto test.
    """
    delay = 0.1
    for attempt in range(attempts):
        proc = git(*args, cwd=cwd, check=False)
        if proc.returncode == 0:
            return proc
        if attempt == attempts - 1:
            raise RuntimeError(
                f"git {' '.join(args)} fallito: {(proc.stderr or '').strip()[:200]}"
            )
        time.sleep(delay)
        delay *= 2


# ==========================================================================
# Fase 1 — costruzione dell'arena
# ==========================================================================

def build_arena(base: Path, users: int, events: int, credit: int) -> dict:
    """Bare repo + stato iniziale + utenti accreditati + eventi aperti."""
    origin = base / "origin.git"
    seed = base / "seed"
    secrets_file = base / "secrets.json"
    seed.mkdir(parents=True)

    shutil.copytree(REPO / "scripts", seed / "scripts")
    (seed / "ledger").mkdir()

    env = dict(os.environ, ARENA_ROOT=str(seed), ARENA_SECRETS_FILE=str(secrets_file))
    os.environ.update(env)

    c.save_json(seed / "events.json", c.default_events())
    c.save_json(seed / "balances.json", c.default_balances())
    c.save_json(seed / "settled.json", c.default_settled())
    c.save_json(seed / "receipts.json", c.default_receipts())
    c.save_json(seed / "ledger" / "state.json", c.default_ledger_state())

    import secrets as pysecrets
    user_ids = [f"u_{i:08x}" for i in range(1, users + 1)]
    secrets_map = {u: pysecrets.token_hex(32) for u in user_ids}
    secrets_file.write_text(json.dumps(secrets_map, indent=2), encoding="utf-8")

    # Seed del denaro e degli eventi: stessa API degli script, un solo commit.
    ws = c.WorkingState(seed)
    for user_id in user_ids:
        ws.append_entry(c.KIND_CREDIT, f"seed-{user_id}", user_id, credit,
                        meta={"reason": "stress seed"})
        row = ws.balance(user_id)
        row["available"] += credit
        row["lifetime_credited"] += credit

    event_ids = []
    for i in range(events):
        event_id = f"ev_{i}"
        event_ids.append(event_id)
        ws.events[event_id] = {
            "id": event_id, "title": f"stress {i}",
            "side_labels": {"yes": "Si", "no": "No"}, "state": "OPEN",
            "open_at": iso(-60), "close_at": iso(120),
            "pool": {"yes": 0, "no": 0}, "bet_count": {"yes": 0, "no": 0},
            "takeout_bps": 300, "min_bet": 1, "max_bet": credit * 100,
            "outcome": None, "resolved_at": None, "settled_at": None,
            "void_if_one_sided": False, "currency": "PTS", "unit_scale": 1,
        }
    # Evento gia' chiuso: serve alle bet ostili (ERR_EVENT_CLOSED).
    ws.events["ev_closed"] = dict(ws.events[event_ids[0]], id="ev_closed",
                                  close_at=iso(-5), pool={"yes": 0, "no": 0},
                                  bet_count={"yes": 0, "no": 0})
    ws.flush()

    git("init", "-q", "-b", "main", ".", cwd=seed)
    git("config", "user.email", "stress@test", cwd=seed)
    git("config", "user.name", "stress", cwd=seed)
    git("add", "-A", cwd=seed)
    git("commit", "-qm", "seed arena", cwd=seed)
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(origin)], check=True)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-q", "-u", "origin", "main", cwd=seed)

    return {
        "origin": origin, "seed": seed, "secrets_file": secrets_file,
        "users": user_ids, "secrets": secrets_map, "events": event_ids,
        "credit": credit,
    }


# ==========================================================================
# Fase 2 — la coda di issue (valide + ostili)
# ==========================================================================

def build_queue(arena: dict, bets_per_user: int, rng: random.Random) -> dict:
    """Genera le issue. Ritorna l'atteso: cosa DEVE finire applicato e cosa no."""
    issues = []
    expect_applied = {}     # bet_id -> amount
    expect_never = {}       # bet_id -> perche' non deve mai entrare nel ledger
    number = 0

    def add_issue(bet, body=None):
        nonlocal number
        number += 1
        text = body if body is not None else (
            f"BET {bet['bet_id']}\n\n```json\n{json.dumps(bet, indent=2)}\n```\n"
        )
        issues.append({"number": number, "body": text, "state": "open"})
        return number

    def signed(user_id, event_id, side, amount, nonce, secret=None):
        bet = {"bet_id": f"{user_id}-{nonce}", "user_id": user_id,
               "event_id": event_id, "side": side, "amount": int(amount),
               "nonce": int(nonce), "ts": c.now_iso()}
        bet["sig"] = c.sign_bet(secret or arena["secrets"][user_id], bet)
        return bet

    # --- bet valide: il budget resta sotto il credito, quindi devono entrare TUTTE.
    # Le bet di un utente vanno messe in coda NELL'ORDINE dei nonce: su GitHub il
    # numero di issue e' monotono e il drain le prende in quell'ordine, quindi un
    # utente non puo' "sorpassare se stesso". Mescolare a caso testerebbe uno
    # scenario che non esiste (e I5 lo rifiuterebbe, giustamente).
    budget = arena["credit"] // (bets_per_user + 1)
    # L'ultimo utente fa da "replayer": il suo nonce 1 viene rifiutato per
    # saldo, quindi NON entra in applied_bets. Quando piu' tardi lo rimanda
    # (firmato bene, importo valido) l'idempotenza non puo' farci nulla: a
    # fermarlo resta solo il nonce monotono (I5). E' l'unico caso in cui I5 e
    # I6 non si coprono a vicenda.
    replayer = arena["users"][-1]
    streams = []
    for user_id in arena["users"]:
        stream = []
        if user_id == replayer:
            stream.append(signed(user_id, arena["events"][0], "yes",
                                 arena["credit"] + 1, 1))
        for nonce in range(2 if user_id == replayer else 1, bets_per_user + 1):
            bet = signed(user_id, rng.choice(arena["events"]),
                         rng.choice(["yes", "no"]), rng.randint(1, budget), nonce)
            expect_applied[bet["bet_id"]] = bet["amount"]
            stream.append(bet)
            # consegna doppia (at-least-once): la copia deve risultare DUP
            if rng.random() < 0.25:
                stream.append(bet)
        if user_id == replayer:
            # stesso bet_id di prima, ma ora l'importo ci starebbe: e' un replay
            stream.append(signed(user_id, arena["events"][0], "yes", 1, 1))
        streams.append(stream)

    # merge casuale dei flussi: gli utenti si interlacciano, ognuno resta in ordine
    while any(streams):
        stream = rng.choice([s for s in streams if s])
        add_issue(stream.pop(0))

    # --- bet ostili: nessuna puo' toccare il ledger, e ognuna deve essere
    # fermata dal controllo GIUSTO. Verificare solo "rifiutata" non basta: un
    # controllo puo' mascherarne un altro (una bet sopra max_bet non arriva mai
    # al controllo di saldo) e il buco resterebbe invisibile.
    victim, attacker = arena["users"][0], arena["users"][1]
    max_bet = arena["credit"] * 100
    expect_never[f"{replayer}-1"] = (
        "replay di un nonce vecchio con bet_id mai applicato (solo I5 lo ferma)",
        c.ERR_NONCE,
    )

    forged = signed(victim, arena["events"][0], "yes", 42,
                    bets_per_user + 50, secret=arena["secrets"][attacker])
    expect_never[forged["bet_id"]] = ("firma forgiata da un altro utente (I10)",
                                      c.ERR_SIG)
    add_issue(forged)

    ghost = {"bet_id": "u_ghost-1", "user_id": "u_ghost",
             "event_id": arena["events"][0], "side": "yes", "amount": 10,
             "nonce": 1, "ts": c.now_iso()}
    ghost["sig"] = c.sign_bet("segreto-inventato", ghost)
    expect_never[ghost["bet_id"]] = ("utente sconosciuto", c.ERR_UNKNOWN_USER)
    add_issue(ghost)

    # credit+1 e' sempre sopra il disponibile (che parte da credit e scende) ma
    # sempre sotto max_bet: cosi' a fermarla e' davvero I4, non i limiti.
    broke = signed(victim, arena["events"][0], "yes", arena["credit"] + 1,
                   bets_per_user + 51)
    expect_never[broke["bet_id"]] = ("saldo insufficiente (I4)", c.ERR_BALANCE)
    add_issue(broke)

    oversize = signed(victim, arena["events"][0], "yes", max_bet + 1,
                      bets_per_user + 52)
    expect_never[oversize["bet_id"]] = ("sopra max_bet", c.ERR_LIMITS)
    add_issue(oversize)

    closed = signed(victim, "ev_closed", "yes", 10, bets_per_user + 53)
    expect_never[closed["bet_id"]] = ("evento chiuso (freeze a close_at)",
                                      c.ERR_EVENT_CLOSED)
    add_issue(closed)

    # Stesso bet_id di una bet valida ma contenuto diverso: chi arriva primo
    # vince, l'altro e' DUP. L'esito dipende dall'ordine, quindi qui non si
    # asserisce il codice: a coprirlo e' il controllo "una sola BET_DEBIT per
    # bet_id" in fase di verifica.
    stale = signed(victim, arena["events"][0], "yes", 7, 1)
    stale["bet_id"] = f"{victim}-1"
    add_issue(stale)

    add_issue(None, body="chiacchiere senza payload")
    add_issue(None, body="```json\n{rotto,,}\n```")

    # Le ostili sono state aggiunte in fondo: le spargo in posizioni casuali
    # senza toccare l'ordine relativo delle bet valide.
    hostile = issues[-8:]
    valid = issues[:-8]
    for issue in hostile:
        valid.insert(rng.randrange(len(valid) + 1), issue)
    issues = valid
    for i, issue in enumerate(issues, 1):   # l'ordine d'arrivo E' il numero
        issue["number"] = i

    return {"issues": issues, "expect_applied": expect_applied,
            "expect_never": expect_never}


# ==========================================================================
# Fase 3 — la tempesta di drain concorrenti
# ==========================================================================

def worker_drain(args) -> int:
    """Un runner: N giri di `drain.py`, ognuno da un checkout aggiornato."""
    clone = Path(args.clone)
    rng = random.Random(args.seed)
    report = {"worker": args.id, "rounds": [], "crashes": 0}

    for round_i in range(args.rounds):
        # Ogni giro riparte da origin: e' il checkout pulito di un nuovo run.
        git_retry("fetch", "-q", "origin", "main", cwd=clone)
        git_retry("reset", "-q", "--hard", "origin/main", cwd=clone)
        git("clean", "-qfd", cwd=clone, check=False)

        cmd = [sys.executable, str(clone / "scripts" / "drain.py"),
               "--issues-file", args.queue, "--close-issues-in-file",
               "--commit", "--limit", str(args.limit), "--max-attempts", "2"]
        env = dict(os.environ, ARENA_ROOT=str(clone),
                   ARENA_SECRETS_FILE=args.secrets,
                   ARENA_TEST_PUSH_DELAY=str(args.push_delay))
        proc = subprocess.Popen(cmd, cwd=str(clone), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        crashed = False
        if rng.random() < args.crash_rate:
            # Crash duro in un punto imprevedibile: durante il calcolo, durante
            # il commit, fra il push e la chiusura delle issue.
            time.sleep(rng.uniform(0.02, 0.5))
            if proc.poll() is None:
                proc.send_signal(signal.SIGKILL)
                crashed = True
                report["crashes"] += 1
        out, err = proc.communicate(timeout=180)
        report["rounds"].append({
            "round": round_i, "exit": proc.returncode, "crashed": crashed,
            "empty": "coda vuota" in out,
            "conflict": "CONFLITTO" in err or "push rifiutato" in err,
        })
        if not crashed and "coda vuota" in out:
            break
        time.sleep(rng.uniform(0.0, 0.05))

    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


def worker_drain_guarded(args) -> int:
    """Il runner non deve morire di traceback: l'errore va scritto nel report."""
    try:
        return worker_drain(args)
    except Exception as exc:  # noqa: BLE001 - qui vogliamo davvero tutto
        import traceback
        Path(args.report).write_text(json.dumps({
            "worker": args.id, "rounds": [], "crashes": 0,
            "fatal": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }, indent=2), encoding="utf-8")
        c.eprint(traceback.format_exc())
        return 1


def worker_settle(args) -> int:
    """Un runner che liquida un evento; ritenta se perde la corsa al push."""
    clone = Path(args.clone)
    report = {"worker": args.id, "attempts": []}
    for attempt in range(args.rounds):
        git_retry("fetch", "-q", "origin", "main", cwd=clone)
        git_retry("reset", "-q", "--hard", "origin/main", cwd=clone)
        git("clean", "-qfd", cwd=clone, check=False)
        proc = subprocess.run(
            [sys.executable, str(clone / "scripts" / "settle.py"),
             "--event", args.event, "--outcome", args.outcome, "--force", "--commit"],
            cwd=str(clone), text=True, capture_output=True,
            env=dict(os.environ, ARENA_ROOT=str(clone),
                     ARENA_SECRETS_FILE=args.secrets,
                     ARENA_TEST_PUSH_DELAY=str(args.push_delay)),
        )
        report["attempts"].append({
            "exit": proc.returncode,
            "dup": "DUP" in proc.stdout,
            "conflict": proc.returncode == c.EXIT_CONFLICT,
        })
        if proc.returncode != c.EXIT_CONFLICT:
            break
    Path(args.report).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


def spawn(role: str, base: Path, arena: dict, idx: int, extra: dict, rounds: int,
          prefix: str = None) -> tuple:
    """Avvia un processo figlio con il suo clone: e' il nostro "nuovo run"."""
    name = f"{prefix or role}{idx}"
    clone = base / name
    if not clone.exists():
        subprocess.run(["git", "clone", "-q", str(arena["origin"]), str(clone)],
                       check=True)
        git("config", "user.email", f"{name}@test", cwd=clone)
        git("config", "user.name", name, cwd=clone)
    report = base / f"report-{name}.json"
    cmd = [sys.executable, str(HERE / "stress_test.py"), "--role", role,
           "--id", str(idx), "--clone", str(clone), "--report", str(report),
           "--secrets", str(arena["secrets_file"]), "--rounds", str(rounds)]
    for key, value in extra.items():
        cmd += [f"--{key}", str(value)]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True), report


# ==========================================================================
# Fase 4 — verifica da un clone pulito
# ==========================================================================

def verify(base: Path, arena: dict, plan: dict, settled_events) -> list:
    """Ricontrolla TUTTO dallo stato pubblicato. Ritorna la lista di violazioni."""
    check = base / "verify"
    shutil.rmtree(check, ignore_errors=True)
    subprocess.run(["git", "clone", "-q", str(arena["origin"]), str(check)], check=True)

    problems = []
    entries = c.read_ledger(check / "ledger")
    balances = c.load_json(check / "balances.json")["balances"]
    events = c.load_json(check / "events.json")["events"]
    settled = c.load_json(check / "settled.json")["settlements"]
    receipts = c.load_json(check / "receipts.json")["receipts"]
    state = c.load_json(check / "ledger" / "state.json")

    # --- I1/I2/I6/I4 con lo stesso strumento della produzione
    sys.path.insert(0, str(check / "scripts"))
    os.environ["ARENA_ROOT"] = str(check)
    import rebuild_balances
    report = rebuild_balances.audit(check)
    for key, label in (("chain", "I1 catena"), ("balances", "I2 saldi"),
                       ("state", "I6 indice"), ("negative", "I4 negativi")):
        problems += [f"{label}: {p}" for p in report[key]]

    # --- I6: ogni bet_id al massimo una BET_DEBIT
    seen = {}
    for entry in entries:
        if entry["kind"] != c.KIND_BET_DEBIT:
            continue
        if entry["id"] in seen:
            problems.append(
                f"I6 doppia spesa: {entry['id']} applicata a seq "
                f"{seen[entry['id']]} e {entry['seq']}"
            )
        seen[entry["id"]] = entry["seq"]

    # --- I5: nonce strettamente crescente per utente, nell'ordine del ledger
    last_nonce = {}
    for entry in entries:
        if entry["kind"] != c.KIND_BET_DEBIT:
            continue
        user_id, nonce = entry["user_id"], entry["nonce"]
        if nonce <= last_nonce.get(user_id, 0):
            problems.append(
                f"I5 nonce non monotono per {user_id}: {nonce} dopo "
                f"{last_nonce[user_id]} (seq {entry['seq']})"
            )
        last_nonce[user_id] = nonce

    # --- I4 nel tempo: il saldo non deve mai passare per un negativo
    running = {}
    for entry in entries:
        row = running.setdefault(entry["user_id"], {"available": 0, "at_risk": 0})
        if entry["kind"] == c.KIND_CREDIT:
            row["available"] += entry["amount"]
        elif entry["kind"] == c.KIND_BET_DEBIT:
            row["available"] -= entry["amount"]
            row["at_risk"] += entry["amount"]
        else:
            row["available"] += entry["amount"]
            row["at_risk"] -= (entry.get("meta") or {}).get("stake", 0)
        if row["available"] < 0 or row["at_risk"] < 0:
            problems.append(
                f"I4 saldo negativo transitorio per {entry['user_id']} "
                f"a seq {entry['seq']}: {row}"
            )

    # --- le bet ostili non devono esistere nel ledger, e devono essere state
    # fermate dal controllo giusto (altrimenti un controllo ne maschera un altro)
    for bet_id, (why, expected_code) in plan["expect_never"].items():
        if bet_id in seen:
            problems.append(f"bet ostile applicata ({why}): {bet_id}")
        receipt = receipts.get(bet_id)
        if receipt is None:
            problems.append(f"bet ostile senza ricevuta ({why}): {bet_id}")
        elif receipt["status"] != "REJECTED":
            problems.append(f"bet ostile con ricevuta {receipt['status']}: {bet_id}")
        elif receipt["code"] != expected_code:
            problems.append(
                f"bet ostile fermata dal controllo sbagliato: {bet_id} ({why}) "
                f"atteso {expected_code}, ottenuto {receipt['code']}"
            )

    # --- ogni bet valida deve essere stata applicata esattamente una volta
    for bet_id in plan["expect_applied"]:
        if bet_id not in seen:
            receipt = receipts.get(bet_id)
            problems.append(
                f"bet valida non applicata: {bet_id} "
                f"(ricevuta: {receipt['code'] if receipt else 'ASSENTE'})"
            )
        elif bet_id not in state["applied_bets"]:
            problems.append(f"bet nel ledger ma non nell'indice: {bet_id}")

    # --- la ricevuta non puo' mentire al client: e' l'unica cosa che l'utente
    # legge. Una bet nel ledger DEVE risultare APPLIED; una bet che nel ledger
    # non c'e' DEVE risultare REJECTED. (E' qui che si vede a cosa serve I6:
    # senza l'indice di idempotenza una consegna doppia riscriverebbe la
    # ricevuta di una bet applicata con un ERR_NONCE.)
    for bet_id in seen:
        receipt = receipts.get(bet_id)
        if receipt is None:
            problems.append(f"bet applicata senza ricevuta: {bet_id}")
        elif receipt["status"] != "APPLIED":
            problems.append(
                f"ricevuta bugiarda: {bet_id} e' nel ledger (seq {seen[bet_id]}) "
                f"ma la ricevuta dice {receipt['status']} {receipt['code']}"
            )
    for bet_id, receipt in receipts.items():
        if receipt["status"] == "APPLIED" and bet_id not in seen:
            problems.append(
                f"ricevuta bugiarda: {bet_id} risulta {receipt['code']} "
                "ma nel ledger non c'e'"
            )

    # --- I8: la cache dei pool combacia col ledger
    pools = {}
    for entry in entries:
        if entry["kind"] == c.KIND_BET_DEBIT:
            pool = pools.setdefault(entry["event_id"], {"yes": 0, "no": 0})
            pool[entry["side"]] += entry["amount"]
    for event_id, event in events.items():
        expected = pools.get(event_id, {"yes": 0, "no": 0})
        if {k: int(v) for k, v in event["pool"].items()} != expected:
            problems.append(
                f"I8 pool disallineato per {event_id}: cache={event['pool']} "
                f"ledger={expected}"
            )

    # --- settlement: uno solo per evento, una sola SETTLE_USER per utente
    per_event = {}
    for entry in entries:
        if entry["kind"] == c.KIND_SETTLE_USER:
            key = (entry["event_id"], entry["user_id"])
            per_event[key] = per_event.get(key, 0) + 1
    for (event_id, user_id), count in per_event.items():
        if count > 1:
            problems.append(
                f"doppio settlement: {count} SETTLE_USER per {user_id} su {event_id}"
            )
    for event_id in settled_events:
        if event_id not in settled:
            problems.append(f"evento {event_id} liquidato ma assente da settled.json")
            continue
        rec = settled[event_id]
        if rec["payout_total"] + rec["takeout"] + rec["dust_to_house"] != rec["T"]:
            problems.append(f"I3 conservazione rotta su {event_id}: {rec}")
        for user_id, row in balances.items():
            if row["at_risk"] < 0:
                problems.append(f"at_risk negativo per {user_id}")

    # --- conservazione globale: credito = saldi + incasso della casa
    credited = sum(e["amount"] for e in entries if e["kind"] == c.KIND_CREDIT)
    held = sum(r["available"] + r["at_risk"] for r in balances.values())
    house = sum(r["house_total"] for r in settled.values())
    if credited != held + house:
        problems.append(
            f"conservazione globale rotta: accreditato={credited} "
            f"saldi={held} casa={house} (delta={credited - held - house})"
        )

    # --- la coda deve essersi svuotata (effetti collaterali convergenti)
    queue = json.loads(Path(base / "queue.json").read_text(encoding="utf-8"))
    still_open = [i["number"] for i in queue if i.get("state", "open") == "open"]
    if still_open:
        problems.append(f"issue ancora aperte a fine corsa: {still_open[:10]}")

    stats = {
        "entries": len(entries), "bet_debit": len(seen),
        "credited": credited, "held": held, "house": house,
        "settled": len(settled), "receipts": len(receipts),
    }
    return problems, stats


# ==========================================================================
# Orchestratore
# ==========================================================================

def orchestrate(args) -> int:
    rng = random.Random(args.seed)
    base = Path(args.workdir) if args.workdir else Path(
        tempfile.mkdtemp(prefix="arena-stress-")
    )
    base.mkdir(parents=True, exist_ok=True)
    log(f"arena: {base}")

    log(f"\n[1/5] costruzione: {args.users} utenti, {args.events} eventi, "
        f"credito {args.credit}")
    arena = build_arena(base, args.users, args.events, args.credit)

    log(f"[2/5] coda: {args.bets} bet a testa + duplicati + bet ostili")
    plan = build_queue(arena, args.bets, rng)
    queue_path = base / "queue.json"
    queue_path.write_text(json.dumps(plan["issues"], indent=2), encoding="utf-8")
    log(f"      {len(plan['issues'])} issue in coda "
        f"({len(plan['expect_applied'])} bet valide, "
        f"{len(plan['expect_never'])} ostili)")

    log(f"[3/5] tempesta: {args.runners} drain concorrenti, "
        f"crash rate {args.crash_rate:.0%}, NESSUNA serializzazione")
    started = time.time()
    procs = []
    for idx in range(args.runners):
        procs.append(spawn("drain", base, arena, idx, {
            "queue": str(queue_path), "limit": args.limit,
            "crash-rate": args.crash_rate, "seed": rng.randrange(10 ** 6),
            "push-delay": args.push_delay,
        }, args.rounds))
    reports = []
    runner_failures = []
    for proc, report_path in procs:
        out, err = proc.communicate(timeout=900)
        if proc.returncode != 0:
            runner_failures.append(err.strip())
            log(f"      \033[31mrunner uscito con {proc.returncode}\033[0m:\n"
                + "\n".join("        " + line for line in err.strip().splitlines()[-12:]))
        if report_path.exists():
            reports.append(json.loads(report_path.read_text(encoding="utf-8")))
    elapsed = time.time() - started

    for report in reports:
        if report.get("fatal"):
            log(f"      runner {report['worker']} fatale: {report['fatal']}")
    rounds = sum(len(r["rounds"]) for r in reports)
    crashes = sum(r["crashes"] for r in reports)
    conflicts = sum(1 for r in reports for x in r["rounds"] if x["conflict"])
    log(f"      {rounds} run di drain in {elapsed:.1f}s "
        f"({crashes} crash, {conflicts} conflitti di push)")
    if runner_failures:
        log(f"      \033[31m{len(runner_failures)} runner morti male\033[0m "
            "(il test continua: la verifica finale dira' se ha fatto danni)")

    # Un ultimo giro pulito: raccoglie quel che i crash hanno lasciato in coda.
    log("[4/5] giro di bonifica + settlement simultanei")
    for _ in range(3):
        proc, _report = spawn("drain", base, arena, 0, {
            "queue": str(queue_path), "limit": args.limit,
            "crash-rate": 0.0, "seed": 1, "push-delay": 0.0,
        }, 3)
        proc.communicate(timeout=300)

    settled_events = arena["events"][: max(1, len(arena["events"]) - 1)]
    settle_procs = []
    for event_id in settled_events:
        for idx in range(args.settlers):
            settle_procs.append(spawn(
                "settle", base, arena, idx,
                {"event": event_id, "outcome": rng.choice(["yes", "no"])}, 4,
                prefix=f"settle-{event_id}-",
            ))
    settle_conflicts = settle_dups = 0
    for proc, report_path in settle_procs:
        proc.communicate(timeout=300)
        if report_path.exists():
            data = json.loads(report_path.read_text(encoding="utf-8"))
            settle_conflicts += sum(1 for a in data["attempts"] if a["conflict"])
            settle_dups += sum(1 for a in data["attempts"] if a["dup"])
    log(f"      {len(settled_events)} eventi liquidati da "
        f"{args.settlers} processi simultanei ciascuno "
        f"({settle_conflicts} conflitti, {settle_dups} no-op idempotenti)")

    log("[5/5] verifica da un clone pulito")
    problems, stats = verify(base, arena, plan, settled_events)

    log("")
    log(f"  entry nel ledger: {stats['entries']}   bet applicate: {stats['bet_debit']}")
    log(f"  accreditato: {stats['credited']}   saldi: {stats['held']}   "
        f"casa: {stats['house']}")
    log(f"  eventi liquidati: {stats['settled']}   ricevute: {stats['receipts']}")

    if problems:
        log(f"\n\033[31m{len(problems)} VIOLAZIONI\033[0m")
        for problem in problems[:40]:
            log(f"  - {problem}")
        if len(problems) > 40:
            log(f"  ... e altre {len(problems) - 40}")
        log(f"\narena conservata per l'analisi: {base}")
        return 1

    log("\n\033[32mNESSUNA VIOLAZIONE\033[0m: catena, saldi, nonce, pool, "
        "idempotenza e conservazione reggono.")
    if args.keep:
        log(f"arena conservata: {base}")
    elif not args.workdir:
        shutil.rmtree(base, ignore_errors=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Stress test di concorrenza")
    ap.add_argument("--users", type=int, default=12)
    ap.add_argument("--bets", type=int, default=4, help="bet valide per utente")
    ap.add_argument("--events", type=int, default=3)
    ap.add_argument("--runners", type=int, default=5, help="drain concorrenti")
    ap.add_argument("--settlers", type=int, default=3, help="settle simultanei per evento")
    ap.add_argument("--rounds", type=int, default=8, help="giri per runner")
    ap.add_argument("--limit", type=int, default=40, help="issue per lotto")
    ap.add_argument("--crash-rate", type=float, default=0.25)
    ap.add_argument("--push-delay", type=float, default=0.15,
                    help="ritardo fra commit e push: allarga la corsa fra scrittori")
    ap.add_argument("--credit", type=int, default=100000)
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--workdir", help="directory di lavoro (default: temporanea)")
    ap.add_argument("--keep", action="store_true", help="non cancellare l'arena")
    # ruoli interni (processi figli)
    ap.add_argument("--role", choices=["drain", "settle"], help=argparse.SUPPRESS)
    ap.add_argument("--id", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--clone", help=argparse.SUPPRESS)
    ap.add_argument("--report", help=argparse.SUPPRESS)
    ap.add_argument("--secrets", help=argparse.SUPPRESS)
    ap.add_argument("--queue", help=argparse.SUPPRESS)
    ap.add_argument("--event", help=argparse.SUPPRESS)
    ap.add_argument("--outcome", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.role == "drain":
        return worker_drain_guarded(args)
    if args.role == "settle":
        return worker_settle(args)
    return orchestrate(args)


if __name__ == "__main__":
    raise SystemExit(main())
