#!/usr/bin/env python3
"""Creazione/apertura di un evento (§6).

    python3 scripts/create_event.py --id ev_derby --title "Roma-Lazio" \
        --close-at 2026-09-20T18:00:00Z [--yes "Roma" --no "Lazio"] \
        [--takeout-bps 300] [--min-bet 100] [--max-bet 1000000]

Equivale a editare `events.json` a mano, ma con i default e la validazione al
posto giusto. Non tocca il ledger: un evento non muove denaro finche' non
arrivano le bet.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Crea un evento OPEN")
    ap.add_argument("--id", dest="event_id", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--close-at", required=True, help="ISO-8601 UTC, es. 2026-09-20T18:00:00Z")
    ap.add_argument("--open-at", help="default: ora")
    ap.add_argument("--yes", dest="yes_label", default="Yes")
    ap.add_argument("--no", dest="no_label", default="No")
    ap.add_argument("--takeout-bps", type=int, default=300)
    ap.add_argument("--min-bet", type=int, default=100)
    ap.add_argument("--max-bet", type=int, default=1000000)
    ap.add_argument("--currency", default="PTS")
    ap.add_argument("--unit-scale", type=int, default=1)
    ap.add_argument("--no-void-if-one-sided", action="store_true",
                    help="non rimborsare se un lato resta a pool 0")
    ap.add_argument("--replace", action="store_true", help="sovrascrivi un evento OPEN")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    ws = c.WorkingState()
    if args.event_id in ws.events and not args.replace:
        c.eprint(f"errore: evento {args.event_id} esiste gia' (usa --replace)")
        return 1
    if args.event_id in ws.events and ws.events[args.event_id].get("state") == "SETTLED":
        c.eprint(f"errore: {args.event_id} e' SETTLED, non si riapre")
        return 1

    try:
        open_at = args.open_at or c.now_iso()
        close_dt = c.parse_iso(args.close_at)
        c.parse_iso(open_at)
    except ValueError as exc:
        c.eprint(f"errore: timestamp non valido ({exc})")
        return 2
    if not 0 <= args.takeout_bps < 10000:
        c.eprint("errore: takeout_bps deve stare in [0, 10000)")
        return 2
    if args.min_bet <= 0 or args.max_bet < args.min_bet:
        c.eprint("errore: limiti incoerenti (serve 0 < min_bet <= max_bet)")
        return 2
    if close_dt <= c.parse_iso(open_at):
        c.eprint("errore: close_at deve seguire open_at")
        return 2

    ws.events[args.event_id] = {
        "id": args.event_id,
        "title": args.title,
        "side_labels": {"yes": args.yes_label, "no": args.no_label},
        "state": "OPEN",
        "open_at": open_at,
        "close_at": args.close_at,
        "pool": {"yes": 0, "no": 0},
        "bet_count": {"yes": 0, "no": 0},
        "takeout_bps": args.takeout_bps,
        "min_bet": args.min_bet,
        "max_bet": args.max_bet,
        "outcome": None,
        "resolved_at": None,
        "settled_at": None,
        "void_if_one_sided": not args.no_void_if_one_sided,
        "currency": args.currency,
        "unit_scale": args.unit_scale,
    }

    c.save_json(ws.root / "events.json", ws.events_doc)
    print(f"OPEN {args.event_id}: \"{args.title}\" chiude {args.close_at} "
          f"takeout={args.takeout_bps}bps limiti=[{args.min_bet},{args.max_bet}]")
    if args.commit:
        c.commit_and_push(
            ["events.json"], f"open event {args.event_id}", push=not args.no_push
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
