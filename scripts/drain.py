#!/usr/bin/env python3
"""Svuota a lotti la coda di Issue con label `bet` (§7).

Cambia solo il CANALE d'ingresso: ledger, HMAC, reducer e settlement restano
quelli di `place_bet.py`, perche' la pipeline di validazione e' la stessa
funzione (`common.validate_and_apply`).

    python3 scripts/drain.py --commit                    # in Actions
    python3 scripts/drain.py --issues-file q.json --no-commit   # offline/test

Exit code: 0 lotto processato (o coda vuota) — le bet rifiutate sono un esito
normale e non rendono rosso il run; 11 conflitto di push persistente (le issue
restano in coda); 1 errore di infrastruttura; 2 configurazione.

Ordine critico (§7.3): PRIMA si committa ledger+ricevute, SOLO DOPO si toccano
le issue. Le issue sono un effetto collaterale non critico: se la chiusura
fallisce, il prossimo drain ri-processa quei `bet_id` e l'idempotenza (I6) li
rende no-op. Consegna at-least-once + apply idempotente = effetto once.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c
from place_bet import extract_json_block

BET_LABEL = "bet"
APPLIED_LABEL = "applied"
REJECTED_LABEL = "rejected"
API = "https://api.github.com"


# --------------------------------------------------------------------------
# Code di issue
# --------------------------------------------------------------------------

class GitHubQueue:
    """Coda reale su GitHub Issues (stdlib: niente dipendenza da `gh`)."""

    def __init__(self, repo: str, token: str, label: str = BET_LABEL):
        if not repo:
            raise SystemExit("errore: GITHUB_REPOSITORY non impostata")
        if not token:
            raise SystemExit("errore: GITHUB_TOKEN non impostata")
        self.repo = repo
        self.token = token
        self.label = label

    def _request(self, method: str, path: str, payload=None):
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "arena-parimutuel-drain")
        if data is not None:
            req.add_header("Content-Type", "application/json")

        delay = 2
        last = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = resp.read().decode("utf-8")
                    link = resp.headers.get("Link", "")
                    return (json.loads(body) if body else None), link
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code < 500 and exc.code != 429:
                    raise RuntimeError(f"{method} {url} -> {exc.code}: {detail}")
                last = f"{exc.code}: {detail}"
            except urllib.error.URLError as exc:
                last = str(exc)
            if attempt < 3:
                time.sleep(delay)
                delay *= 2
        raise RuntimeError(f"{method} {url} fallita dopo 4 tentativi: {last}")

    def list_open(self, limit: int):
        """Issue aperte con la label, in ordine di numero crescente (= arrivo)."""
        out = []
        page = 1
        while len(out) < limit:
            query = urllib.parse.urlencode(
                {
                    "labels": self.label,
                    "state": "open",
                    "sort": "created",
                    "direction": "asc",
                    "per_page": 100,
                    "page": page,
                }
            )
            batch, _ = self._request("GET", f"/repos/{self.repo}/issues?{query}")
            if not batch:
                break
            for issue in batch:
                if "pull_request" in issue:
                    continue  # le PR passano dallo stesso endpoint
                out.append({"number": issue["number"], "body": issue.get("body") or ""})
            if len(batch) < 100:
                break
            page += 1
        out.sort(key=lambda i: i["number"])
        return out[:limit]

    def comment(self, number: int, body: str):
        self._request("POST", f"/repos/{self.repo}/issues/{number}/comments",
                      {"body": body})

    def label_and_close(self, number: int, label: str):
        self._request("POST", f"/repos/{self.repo}/issues/{number}/labels",
                      {"labels": [label]})
        self._request("PATCH", f"/repos/{self.repo}/issues/{number}",
                      {"state": "closed", "state_reason": "completed"})


class FileQueue:
    """Coda su file: stesso contratto, nessuna rete. Usata dai test.

    Con `mutate=True` la chiusura viene scritta davvero nel file della coda,
    sotto lock: serve allo stress test, dove piu' processi concorrenti devono
    vedere la stessa coda che si svuota, esattamente come le Issue su GitHub.
    """

    def __init__(self, path: Path, effects: Path = None, mutate: bool = False):
        self.path = Path(path)
        self.effects_path = Path(effects) if effects else None
        self.mutate = mutate
        self.effects = []

    @contextlib.contextmanager
    def _lock(self):
        """Lock d'avviso su un file a parte: il JSON non si tronca mai a meta'."""
        if not self.mutate:
            yield
            return
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def list_open(self, limit: int):
        with self._lock():
            data = self._read()
        issues = [
            {"number": int(i["number"]), "body": i.get("body") or ""}
            for i in data
            if i.get("state", "open") == "open"
        ]
        issues.sort(key=lambda i: i["number"])
        return issues[:limit]

    def comment(self, number: int, body: str):
        self.effects.append({"op": "comment", "number": number, "body": body})

    def label_and_close(self, number: int, label: str):
        self.effects.append({"op": "close", "number": number, "label": label})
        if not self.mutate:
            return
        with self._lock():
            data = self._read()
            for issue in data:
                if int(issue["number"]) == int(number):
                    issue["state"] = "closed"
                    issue["label"] = label
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)

    def dump(self):
        if self.effects_path:
            self.effects_path.write_text(
                json.dumps(self.effects, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )


# --------------------------------------------------------------------------
# Drain
# --------------------------------------------------------------------------

def receipt_comment(bet_id, receipt: dict) -> str:
    lines = [
        f"**{receipt['status']} `{receipt['code']}`**",
        "",
        f"- bet_id: `{bet_id or 'n/d'}`",
        f"- ts: `{receipt['ts']}`",
    ]
    if receipt.get("seq") is not None:
        lines.append(f"- seq: `{receipt['seq']}`")
    if receipt.get("available") is not None:
        lines.append(f"- available: `{receipt['available']}`")
    if receipt.get("reason"):
        lines.append(f"- reason: {receipt['reason']}")
    lines += ["", "_La ricevuta autorevole e' in `receipts.json` (chiave `bet_id`)._"]
    return "\n".join(lines)


def parse_issue_body(body: str):
    block = extract_json_block(body or "")
    if block is None:
        raise ValueError("nessun blocco ```json``` nel corpo della issue")
    return json.loads(block)


def drain(queue, ws: c.WorkingState, secrets: dict, limit: int):
    """Processa il lotto in memoria. Ritorna (risultati, issue) senza scrivere."""
    issues = queue.list_open(limit)
    processed = []
    for issue in issues:
        try:
            bet = parse_issue_body(issue["body"])
        except (ValueError, json.JSONDecodeError) as exc:
            receipt = c.make_receipt(c.ERR_MALFORMED, reason=str(exc)[:200])
            processed.append((issue, c.BetResult(None, c.ERR_MALFORMED, receipt)))
            continue
        # Stessa identica pipeline del canale dispatch (§8); la copia di lavoro
        # e' aggiornata bet dopo bet, quindi due bet dello stesso utente nello
        # stesso lotto vedono saldo e nonce corretti.
        processed.append((issue, c.validate_and_apply(bet, ws, secrets)))
    return processed


def run_batch(queue, secrets, args):
    """Un tentativo completo: stato fresco da disco, lotto, commit.

    Rilegge SEMPRE lo stato all'inizio: e' quello che rende ripetibile un
    tentativo dopo un push rifiutato.
    """
    ws = c.WorkingState()
    processed = drain(queue, ws, secrets, args.limit)
    if not processed:
        return []

    applied = sum(1 for _, r in processed if r.applied)
    dups = sum(1 for _, r in processed if r.code == c.DUP)
    rejected = len(processed) - applied - dups
    for issue, res in processed:
        print(f"#{issue['number']} {res.summary()}")
    print(f"lotto: {len(processed)} issue -> {applied} applicate, {dups} duplicate, "
          f"{rejected} rifiutate")

    # --- COMMIT: ledger, saldi, ricevute. Da qui l'effetto e' durevole. ---
    paths = ws.flush()
    if args.commit:
        c.commit_and_push(
            paths,
            f"drain: {applied} bet applicate, {dups} dup, {rejected} rifiutate",
            push=not args.no_push,
        )
    return processed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Svuota la coda di Issue `bet`")
    ap.add_argument("--limit", type=int, default=200, help="max issue per lotto")
    ap.add_argument("--label", default=BET_LABEL)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--issues-file", help="coda da file JSON (offline/test)")
    ap.add_argument("--effects-file", help="dove registrare gli effetti (con --issues-file)")
    ap.add_argument("--close-issues-in-file", action="store_true",
                    help="con --issues-file: scrive davvero la chiusura nella coda")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    ap.add_argument("--skip-issue-updates", action="store_true",
                    help="non commentare/chiudere (le issue restano in coda)")
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="ritentativi dopo un push rifiutato (0 = nessuno)")
    args = ap.parse_args(argv)

    if args.issues_file:
        queue = FileQueue(args.issues_file, args.effects_file,
                          mutate=args.close_issues_in_file)
    else:
        queue = GitHubQueue(args.repo, os.environ.get("GITHUB_TOKEN", ""), args.label)

    try:
        secrets = c.load_user_secrets()
    except c.SecretsError as exc:
        c.eprint(f"errore di configurazione: {exc}")
        return c.EXIT_USAGE

    # Un push rifiutato significa che il remoto e' avanzato mentre calcolavamo:
    # il lotto va RIFATTO da capo sullo stato nuovo, non riproposto. Chi ha
    # vinto la corsa ha gia' applicato parte delle bet: al secondo giro quelle
    # diventano DUP e le altre si applicano sopra i saldi aggiornati.
    attempts = max(1, args.max_attempts)
    processed = None
    for attempt in range(1, attempts + 1):
        try:
            processed = run_batch(queue, secrets, args)
            break
        except c.PushRejected as exc:
            c.eprint(f"tentativo {attempt}/{attempts}: {exc}")
            if attempt == attempts:
                c.eprint(
                    "conflitto persistente: le issue restano aperte, "
                    "il prossimo drain (cron) le riprende"
                )
                return c.EXIT_CONFLICT
            try:
                head = c.reset_to_remote()
            except (RuntimeError, OSError) as exc:
                c.eprint(f"impossibile riallinearsi a origin: {exc}")
                return c.EXIT_ERROR
            c.eprint(f"ricalcolo il lotto su HEAD aggiornato ({head[:8]})")
        except (RuntimeError, OSError) as exc:
            # Il commit non e' passato: NON si toccano le issue, cosi' restano
            # in coda per il prossimo giro.
            c.eprint(f"errore di infrastruttura: {exc}")
            return c.EXIT_ERROR

    if not processed:
        print("coda vuota, no-op")
        return c.EXIT_OK

    # --- SOLO ORA le issue: effetto collaterale, best effort. ---
    if not args.skip_issue_updates:
        for issue, res in processed:
            number = issue["number"]
            try:
                queue.comment(number, receipt_comment(res.bet_id, res.receipt))
                queue.label_and_close(number, APPLIED_LABEL if res.ok else REJECTED_LABEL)
            except (RuntimeError, OSError) as exc:
                # Non e' un fallimento del drain: la ricevuta e' gia' committata
                # e il prossimo giro richiudera' la issue (no-op idempotente).
                c.eprint(f"avviso: aggiornamento issue #{number} fallito: {exc}")
    if isinstance(queue, FileQueue):
        queue.dump()
    return c.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
