#!/usr/bin/env python3
"""Ingestione di UNA bet (canale `workflow_dispatch`).

Canale semplice, adatto a pochi utenti: un run per scommessa. Per il
multiutente a scala si usa `drain.py` (coda di Issue), che riusa esattamente
la stessa pipeline di validazione — qui non vive nessuna regola propria.

    python3 scripts/place_bet.py --json '{"bet_id":...}' [--commit]
    python3 scripts/place_bet.py --file bet.json
    BET_JSON='{"bet_id":...}' python3 scripts/place_bet.py

Exit code: 0 se la bet e' stata applicata (o era un duplicato), 1 se rifiutata.
La ricevuta finisce sempre in `receipts.json` — e' li' che guarda il client.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def parse_bet(text: str):
    """Accetta sia JSON nudo sia un blocco ```json ... ``` (come nelle issue)."""
    text = (text or "").strip()
    if not text:
        raise ValueError("payload vuoto")
    if "```" in text:
        block = extract_json_block(text)
        if block is None:
            raise ValueError("nessun blocco ```json``` nel payload")
        text = block
    return json.loads(text)


def extract_json_block(body: str):
    """Estrae il primo blocco ```json ... ``` (fallback: primo blocco ```)."""
    fences = []
    idx = 0
    while True:
        start = body.find("```", idx)
        if start < 0:
            break
        newline = body.find("\n", start)
        if newline < 0:
            break
        lang = body[start + 3 : newline].strip().lower()
        end = body.find("```", newline + 1)
        if end < 0:
            break
        fences.append((lang, body[newline + 1 : end]))
        idx = end + 3
    for lang, content in fences:
        if lang in ("json", "json5", ""):
            return content
    return fences[0][1] if fences else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Applica una singola bet firmata")
    ap.add_argument("--json", help="payload della bet")
    ap.add_argument("--file", help="file contenente il payload")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    raw = args.json
    if args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    if raw is None:
        raw = os.environ.get("BET_JSON")
    if raw is None:
        c.eprint("errore: serve --json, --file oppure BET_JSON")
        return 2

    ws = c.WorkingState()
    try:
        bet = parse_bet(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        # Payload illeggibile: nessun bet_id affidabile su cui scrivere una
        # ricevuta, quindi resta solo il log del run.
        c.eprint(f"REJECTED {c.ERR_MALFORMED}: {exc}")
        return 1

    try:
        secrets = c.load_user_secrets()
    except c.SecretsError as exc:
        c.eprint(f"errore di configurazione: {exc}")
        return 2

    res = c.validate_and_apply(bet, ws, secrets)
    paths = ws.flush()
    print(res.summary())

    if args.commit:
        subject = f"bet {res.bet_id or '<malformed>'}: {res.code}"
        c.commit_and_push(paths, subject, push=not args.no_push)

    return 0 if res.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
