#!/usr/bin/env python3
"""Liquidazione parimutuel di un evento (§9).

    python3 scripts/settle.py --event ev_derby --outcome yes [--commit]
    python3 scripts/settle.py --event ev_derby --outcome void     # rimborso totale

Regole non negoziabili:

* i pool sono ricalcolati dalle `BET_DEBIT` del ledger, mai dalla cache
  `events.pool` (I8);
* gli assert di conservazione (I3) girano PRIMA di qualunque scrittura: se
  falliscono il run abortisce senza toccare un byte;
* una sola `SETTLE_USER` aggregata per utente, anche per i perdenti, cosi'
  `at_risk` torna a zero per tutti.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


class Abort(RuntimeError):
    """Violazione di invariante: si esce senza scrivere."""


def collect_stakes(ledger_base: Path, event_id: str):
    """(stakes, S) dalle sole BET_DEBIT dell'evento — fonte di verita' (I8)."""
    stakes = {"yes": {}, "no": {}}
    totals = {"yes": 0, "no": 0}
    for entry in c.iter_ledger(ledger_base):
        if entry.get("kind") != c.KIND_BET_DEBIT or entry.get("event_id") != event_id:
            continue
        side = entry.get("side")
        if side not in c.SIDES:
            raise Abort(f"BET_DEBIT seq={entry.get('seq')} con side {side!r}")
        user_id = entry["user_id"]
        amount = int(entry["amount"])
        stakes[side][user_id] = stakes[side].get(user_id, 0) + amount
        totals[side] += amount
    return stakes, totals


def compute_settlement(stakes, totals, outcome, takeout_bps, void_if_one_sided):
    """Matematica pura del settlement: nessun I/O, tutto intero.

    Ritorna un dict con payout per utente e le cifre del record §3.5.
    """
    T = totals["yes"] + totals["no"]
    users = sorted(set(stakes["yes"]) | set(stakes["no"]))
    stake_of = {
        u: stakes["yes"].get(u, 0) + stakes["no"].get(u, 0) for u in users
    }

    one_sided = totals["yes"] == 0 or totals["no"] == 0
    no_winners = outcome in c.SIDES and totals[outcome] == 0
    refund_all = outcome == "void" or no_winners or (void_if_one_sided and one_sided)

    if refund_all:
        payouts = {u: stake_of[u] for u in users}
        takeout = 0
        distributable = T
        winning_side = None
        winning_pool = 0
        dust = 0
    else:
        winning_side = outcome
        winning_pool = totals[outcome]
        takeout = T * takeout_bps // 10000
        distributable = T - takeout
        payouts = {}
        for user_id in users:
            stake_win = stakes[outcome].get(user_id, 0)
            payouts[user_id] = stake_win * distributable // winning_pool if stake_win else 0
        dust = distributable - sum(payouts.values())

    payout_total = sum(payouts.values())
    house_total = takeout + dust

    # --- I3: conservazione. Abortire qui costa un run; sbagliare costa i soldi.
    if refund_all:
        if payout_total != T:
            raise Abort(f"rimborso non conservativo: Σpayout={payout_total} != T={T}")
        if any(payouts[u] != stake_of[u] for u in users):
            raise Abort("rimborso: qualche payout != stake")
    else:
        if payout_total > distributable:
            raise Abort(
                f"payout_total={payout_total} > distributable={distributable}"
            )
        if payout_total + takeout + dust != T:
            raise Abort(
                f"conservazione rotta: {payout_total}+{takeout}+{dust} != {T}"
            )
        if dust < 0:
            raise Abort(f"dust negativo: {dust}")
    if any(v < 0 for v in payouts.values()):
        raise Abort("payout negativo")
    if takeout < 0 or house_total < 0:
        raise Abort("takeout/house negativi")

    return {
        "users": users,
        "payouts": payouts,
        "stakes": stake_of,
        "stakes_by_side": stakes,
        "T": T,
        "void": bool(refund_all),
        "takeout": takeout,
        "distributable": distributable,
        "winning_side": winning_side,
        "winning_pool": winning_pool,
        "payout_total": payout_total,
        "dust_to_house": dust,
        "house_total": house_total,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Liquida un evento parimutuel")
    ap.add_argument("--event", required=True, help="event_id")
    ap.add_argument("--outcome", required=True, choices=c.OUTCOMES)
    ap.add_argument("--force", action="store_true",
                    help="liquida anche prima di close_at (override owner)")
    ap.add_argument("--dry-run", action="store_true", help="calcola e stampa, non scrive")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    ws = c.WorkingState()
    event_id = args.event

    # --- precondizioni -----------------------------------------------------
    if event_id in ws.settlements:
        rec = ws.settlements[event_id]
        print(
            f"DUP {event_id}: gia' liquidato il {rec['settled_at']} "
            f"(outcome={rec['outcome']}, void={rec['void']}), no-op"
        )
        return 0

    event = ws.events.get(event_id)
    if event is None:
        c.eprint(f"errore: evento sconosciuto: {event_id}")
        return 1
    if event.get("state") == "SETTLED":
        c.eprint(
            f"errore: {event_id} e' SETTLED in events.json ma assente da settled.json: "
            "stato incoerente, usa rebuild_balances.py prima di procedere"
        )
        return 1

    now = c.now_iso()
    if args.outcome != "void" and not args.force:
        try:
            if c.parse_iso(now) < c.parse_iso(event["close_at"]):
                c.eprint(
                    f"errore: {event_id} chiude alle {event['close_at']} "
                    f"(ora {now}); usa --outcome void oppure --force"
                )
                return 1
        except (KeyError, ValueError):
            c.eprint(f"errore: close_at mancante o non valido su {event_id}")
            return 1

    # --- calcolo -----------------------------------------------------------
    try:
        stakes, totals = collect_stakes(ws.root / "ledger", event_id)
        result = compute_settlement(
            stakes,
            totals,
            args.outcome,
            int(event.get("takeout_bps", 0)),
            bool(event.get("void_if_one_sided", True)),
        )
    except Abort as exc:
        c.eprint(f"ABORT (invariante violata, nulla e' stato scritto): {exc}")
        return 1

    cached = event.get("pool", {})
    if int(cached.get("yes", 0)) != totals["yes"] or int(cached.get("no", 0)) != totals["no"]:
        c.eprint(
            f"attenzione: cache pool {cached} != ledger {totals}; "
            "vale il ledger (I8), la cache viene riallineata"
        )

    print(
        f"{event_id}: outcome={args.outcome} void={result['void']} "
        f"pool={totals} T={result['T']} takeout={result['takeout']} "
        f"distributable={result['distributable']} payout_total={result['payout_total']} "
        f"dust={result['dust_to_house']} house={result['house_total']}"
    )
    for user_id in result["users"]:
        print(
            f"  {user_id}: stake={result['stakes'][user_id]} "
            f"payout={result['payouts'][user_id]}"
        )

    if args.dry_run:
        print("dry-run: nessuna scrittura")
        return 0

    # --- scrittura ---------------------------------------------------------
    for user_id in result["users"]:
        stake = result["stakes"][user_id]
        payout = result["payouts"][user_id]
        ws.append_entry(
            c.KIND_SETTLE_USER,
            entry_id=f"{event_id}::settle::{user_id}",
            user_id=user_id,
            amount=payout,
            event_id=event_id,
            ts=now,
            meta={
                "stake": stake,
                "stake_yes": result["stakes_by_side"]["yes"].get(user_id, 0),
                "stake_no": result["stakes_by_side"]["no"].get(user_id, 0),
                "outcome": args.outcome,
                "void": result["void"],
            },
        )
        row = ws.balance(user_id)
        row["available"] += payout
        row["at_risk"] -= stake

    negative = [u for u in result["users"] if ws.balance(u)["at_risk"] < 0]
    if negative:
        # Non dovrebbe accadere: significherebbe stake contati due volte.
        c.eprint(f"ABORT: at_risk negativo per {negative}; nulla scritto su disco")
        return 1

    event["pool"] = {"yes": totals["yes"], "no": totals["no"]}
    event["state"] = "SETTLED"
    event["outcome"] = args.outcome
    event["resolved_at"] = now
    event["settled_at"] = now

    ws.settlements[event_id] = {
        "event_id": event_id,
        "outcome": args.outcome,
        "void": result["void"],
        "settled_at": now,
        "pool": {"yes": totals["yes"], "no": totals["no"]},
        "T": result["T"],
        "takeout_bps": int(event.get("takeout_bps", 0)),
        "takeout": result["takeout"],
        "distributable": result["distributable"],
        "winning_side": result["winning_side"],
        "winning_pool": result["winning_pool"],
        "payout_total": result["payout_total"],
        "dust_to_house": result["dust_to_house"],
        "house_total": result["house_total"],
        "last_seq": ws.state["last_seq"],
    }

    paths = ws.flush()
    if args.commit:
        c.commit_and_push(
            paths, f"settle {event_id} -> {args.outcome}", push=not args.no_push
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
