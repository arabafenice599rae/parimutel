#!/usr/bin/env python3
"""Provisioning di un utente (§5).

    python3 scripts/register.py --repo owner/arena --print-config          # repo privato
    python3 scripts/register.py --repo owner/arena --recipient age1xyz...  # repo pubblico

Cosa fa:
  1. genera `user_id` (non collidente) e `secret` a 256 bit;
  2. lo aggiunge al magazzino segreti (§5.2: `secrets.age` o file locale);
  3. crea la riga saldo a zero in `balances.json`;
  4. emette il `config.json` dell'utente sul canale scelto (§5.4).

Il segreto non deve MAI finire in un log pubblico: o il repo e' privato (e
allora `--print-config` va bene), oppure si cifra verso la chiave dell'owner
con `--recipient`. Senza una delle due scelte lo script si rifiuta di partire.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def new_user_id(taken) -> str:
    while True:
        candidate = "u_" + pysecrets.token_hex(4)
        if candidate not in taken:
            return candidate


def age_encrypt(text: str, recipient: str) -> str:
    try:
        proc = subprocess.run(
            ["age", "--encrypt", "--armor", "--recipient", recipient],
            input=text.encode("utf-8"),
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        raise SystemExit("errore: binario `age` non trovato (serve per --recipient)")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "errore: age fallito: " + (exc.stderr or b"").decode("utf-8", "replace")
        )
    return proc.stdout.decode("utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Registra un nuovo utente")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""),
                    help="owner/nome del repo, finisce nel config dell'utente")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--read", choices=["raw", "api"], default="raw",
                    help="come il client legge lo stato (Tier 0 = raw)")
    ap.add_argument("--user-id", help="forza uno user_id (default: generato)")
    ap.add_argument("--note", default="", help="etichetta operativa, non pubblicata")
    ap.add_argument("--recipient", help="chiave pubblica age: cifra il config (repo pubblico)")
    ap.add_argument("--print-config", action="store_true",
                    help="stampa il config in chiaro (SOLO se i log sono privati)")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    if not args.recipient and not args.print_config:
        c.eprint(
            "errore: scegli un canale di consegna (§5.4): --recipient <age pubkey> "
            "se i log del run sono pubblici, --print-config se il repo e' privato"
        )
        return 2

    ws = c.WorkingState()
    try:
        secrets_map = c.load_user_secrets()
    except c.SecretsError as exc:
        secrets_map = {}
        c.eprint(f"avviso: magazzino segreti non leggibile ({exc})")

    taken = set(secrets_map) | set(ws.balances)
    user_id = c.clean_id(args.user_id) or new_user_id(taken)
    if user_id in taken:
        c.eprint(f"errore: {user_id} esiste gia'")
        return 1

    secret = pysecrets.token_hex(32)
    secrets_map[user_id] = secret

    stored = None
    try:
        stored = c.save_user_secrets(secrets_map)
    except c.SecretsError as exc:
        c.eprint(
            f"\nATTENZIONE: magazzino non scrivibile ({exc}).\n"
            "Registrazione semi-manuale: aggiungi questa coppia all'Actions "
            "secret USER_SECRETS, altrimenti le bet di questo utente daranno "
            "ERR_UNKNOWN_USER."
        )
        if args.print_config:
            c.eprint(json.dumps({user_id: secret}, indent=2))

    ws.balance(user_id)  # riga a zero (§5.1 punto 4)
    c.save_json(ws.root / "balances.json", ws.balances_doc)

    config = {
        "user_id": user_id,
        "secret": secret,
        "repo": args.repo,
        "branch": args.branch,
        "read": args.read,
    }
    config_text = json.dumps(config, indent=2, sort_keys=True) + "\n"

    print(f"registrato {user_id} (magazzino segreti: {stored or 'MANUALE'})")
    if args.note:
        print(f"nota: {args.note}")
    print("--- config.json per l'utente: salvalo in ~/Documents/.arena/config.json ---")
    if args.recipient:
        print(age_encrypt(config_text, args.recipient))
        print("(cifrato: decifra in locale con `age -d -i <tua-chiave>`)")
    else:
        print(config_text)

    paths = ["balances.json"]
    if stored == "age":
        paths.append("secrets.age")
    if args.commit:
        return c.commit_and_push_cli(paths, f"register {user_id}",
                                     push=not args.no_push)
    return c.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
