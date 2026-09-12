#!/usr/bin/env python3
"""Accredito / rettifica di saldo (owner).

    python3 scripts/credit.py --user u_ab12ef34 --amount 10000 [--reason "seed"]

`amount` puo' essere negativo (rettifica). L'idempotenza e' per `--id`: due run
con lo stesso id non accreditano due volte.
"""

from __future__ import annotations

import argparse
import secrets as pysecrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Accredita (o rettifica) un saldo")
    ap.add_argument("--user", required=True, help="user_id destinatario")
    ap.add_argument("--amount", required=True, type=int, help="intero, anche negativo")
    ap.add_argument("--reason", default="", help="nota operativa (finisce in meta)")
    ap.add_argument("--id", dest="credit_id", help="id idempotente dell'accredito")
    ap.add_argument("--allow-negative", action="store_true",
                    help="consenti un addebito che porta available sotto zero")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    if args.amount == 0:
        c.eprint("errore: amount 0 non ha effetto")
        return 2

    credit_id = args.credit_id or "cr_" + pysecrets.token_hex(8)
    ws = c.WorkingState()

    # Idempotenza: un id gia' presente nel ledger e' un no-op (I6 vale anche qui).
    if c.find_entry_by_id(credit_id, ws.root / "ledger") is not None:
        print(f"DUP {credit_id}: accredito gia' presente nel ledger, no-op")
        return 0

    row = ws.balance(args.user)
    if args.amount < 0 and not args.allow_negative:
        if row["available"] + args.amount < 0:
            c.eprint(
                f"errore: rettifica {args.amount} porterebbe available a "
                f"{row['available'] + args.amount} (usa --allow-negative per forzare)"
            )
            return 1

    meta = {"reason": args.reason} if args.reason else None
    entry = ws.append_entry(
        c.KIND_CREDIT,
        entry_id=credit_id,
        user_id=args.user,
        amount=args.amount,
        meta=meta,
    )
    row["available"] += args.amount
    if args.amount > 0:
        row["lifetime_credited"] += args.amount

    paths = ws.flush()
    print(
        f"CREDIT {credit_id}: {args.user} {args.amount:+d} -> available="
        f"{row['available']} (seq={entry['seq']})"
    )

    if args.commit:
        c.commit_and_push(
            paths, f"credit {args.user} {args.amount:+d} ({credit_id})",
            push=not args.no_push,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
