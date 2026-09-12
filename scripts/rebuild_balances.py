#!/usr/bin/env python3
"""Ricostruzione dei saldi dal solo ledger — strumento di recovery/disputa.

    python3 scripts/rebuild_balances.py            # verifica (exit 1 se disallineato)
    python3 scripts/rebuild_balances.py --write    # riscrive la proiezione
    python3 scripts/rebuild_balances.py --json     # output machine-readable

Verifica:
  I1  catena del ledger (seq contigui, prev_hash, hash ricomputabili)
  I2  balances.json == reduce_balances(ledger)
  I6  ledger/state.json (last_seq, head_hash, applied_bets) coerente col ledger

Le righe interamente a zero non contano come disallineamento: `register` le
crea per comodita' di UI e non portano informazione.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def audit(base: Path = None):
    base = Path(base) if base is not None else c.root()
    entries = c.read_ledger(base / "ledger")

    report = {"entries": len(entries), "chain": [], "balances": [], "state": []}
    report["chain"] = c.verify_chain(entries)

    rebuilt, last_seq, head = c.reduce_balances(entries)
    report["rebuilt"] = rebuilt
    report["last_seq"] = last_seq
    report["head_hash"] = head

    projection = c.load_json(base / "balances.json", c.default_balances())
    report["balances"] = c.diff_balances(projection.get("balances", {}), rebuilt)
    if projection.get("as_of_seq") != last_seq:
        report["balances"].append(
            f"as_of_seq: proiezione={projection.get('as_of_seq')} ledger={last_seq}"
        )
    if projection.get("head_hash") != head:
        report["balances"].append(
            f"head_hash: proiezione={projection.get('head_hash')} ledger={head}"
        )

    state = c.load_json(base / "ledger/state.json", c.default_ledger_state())
    if state.get("last_seq") != last_seq:
        report["state"].append(
            f"last_seq: state={state.get('last_seq')} ledger={last_seq}"
        )
    if state.get("head_hash") != head:
        report["state"].append(
            f"head_hash: state={state.get('head_hash')} ledger={head}"
        )

    applied = state.get("applied_bets", {})
    bets = {e["id"]: e["seq"] for e in entries if e.get("kind") == c.KIND_BET_DEBIT}
    for bet_id in sorted(set(applied) | set(bets)):
        if bet_id not in bets:
            report["state"].append(f"applied_bets[{bet_id}]: nel'indice ma non nel ledger")
        elif bet_id not in applied:
            report["state"].append(f"{bet_id}: BET_DEBIT nel ledger ma non nell'indice")
        elif applied[bet_id] != bets[bet_id]:
            report["state"].append(
                f"applied_bets[{bet_id}]: indice={applied[bet_id]} ledger={bets[bet_id]}"
            )

    negative = sorted(
        f"{u}: available={r['available']} at_risk={r['at_risk']}"
        for u, r in rebuilt.items()
        if r["available"] < 0 or r["at_risk"] < 0
    )
    report["negative"] = negative
    report["ok"] = not (
        report["chain"] or report["balances"] or report["state"] or negative
    )
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica/ricostruisce i saldi dal ledger")
    ap.add_argument("--write", action="store_true",
                    help="riscrive balances.json con la ricostruzione")
    ap.add_argument("--json", action="store_true", help="report JSON su stdout")
    ap.add_argument("--commit", action="store_true", help="git commit + push (con --write)")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    report = audit()

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"entry nel ledger: {report['entries']}  last_seq={report['last_seq']}")
        print(f"head_hash: {report['head_hash']}")
        for label, key in (
            ("I1 catena", "chain"),
            ("I2 saldi", "balances"),
            ("I6 state", "state"),
            ("I4 negativi", "negative"),
        ):
            problems = report[key]
            if problems:
                print(f"{label}: {len(problems)} PROBLEMI")
                for p in problems:
                    print(f"  - {p}")
            else:
                print(f"{label}: OK")

    if args.write:
        doc = c.load_json(c.balances_path(), c.default_balances())
        merged = dict(doc.get("balances", {}))
        for user_id in list(merged):
            merged[user_id] = report["rebuilt"].get(user_id, c.zero_balance())
        merged.update(report["rebuilt"])
        doc["version"] = c.SCHEMA_VERSION
        doc["balances"] = merged
        doc["as_of_seq"] = report["last_seq"]
        doc["head_hash"] = report["head_hash"]
        doc["updated_at"] = c.now_iso()
        c.save_json(c.balances_path(), doc)
        print("balances.json riscritto dal ledger")
        if args.commit:
            return c.commit_and_push_cli(
                ["balances.json"], "rebuild balances from ledger", push=not args.no_push
            )
        return c.EXIT_OK

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
