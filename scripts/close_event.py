#!/usr/bin/env python3
"""Chiusura anticipata della finestra di puntata (owner).

    python3 scripts/close_event.py --event ev_derby [--commit]

Nel modello la finestra si chiude da sola a `close_at` (§6): la validazione
rifiuta le bet con `ERR_EVENT_CLOSED` e il settlement legge solo quelle gia'
entrate. Questo comando serve quando la realta' anticipa il calendario — la
partita comincia prima, la notizia esce prima — e si vuole bloccare subito le
puntate senza aspettare l'ora scritta.

Non tocca il ledger: sposta solo `close_at` a adesso. Le bet gia' applicate
restano tutte valide; quelle che arrivano dopo prendono `ERR_EVENT_CLOSED`.
Lo spostamento resta tracciato in `closed_early_at` per chi guardera' i conti
dopo.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Chiude subito la finestra di un evento")
    ap.add_argument("--event", required=True, help="event_id")
    ap.add_argument("--commit", action="store_true", help="git commit + push")
    ap.add_argument("--no-push", action="store_true", help="commit senza push")
    args = ap.parse_args(argv)

    ws = c.WorkingState()
    event_id = c.clean_id(args.event)
    evento = ws.events.get(event_id)
    if evento is None:
        c.eprint(f"errore: evento sconosciuto: {event_id}")
        return c.EXIT_ERROR
    if evento.get("state") == "SETTLED":
        c.eprint(f"errore: {event_id} e' gia' liquidato")
        return c.EXIT_ERROR

    adesso = c.now_iso()
    try:
        gia_chiuso = c.parse_iso(adesso) >= c.parse_iso(evento["close_at"])
    except (KeyError, ValueError):
        c.eprint(f"errore: close_at mancante o non valido su {event_id}")
        return c.EXIT_ERROR
    if gia_chiuso:
        print(f"DUP {event_id}: finestra gia' chiusa alle {evento['close_at']}, no-op")
        return c.EXIT_OK

    precedente = evento["close_at"]
    evento["close_at"] = adesso
    evento["closed_early_at"] = adesso
    evento["close_at_original"] = precedente

    c.save_json(ws.root / "events.json", ws.events_doc)
    pool = evento.get("pool", {})
    print(f"CHIUSO {event_id}: finestra chiusa alle {adesso} "
          f"(era {precedente}); pool yes={pool.get('yes', 0)} no={pool.get('no', 0)}")
    print("Le bet successive prenderanno ERR_EVENT_CLOSED; ora si puo' liquidare "
          "senza --force.")

    if args.commit:
        return c.commit_and_push_cli(
            ["events.json"], f"close {event_id}", push=not args.no_push)
    return c.EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
