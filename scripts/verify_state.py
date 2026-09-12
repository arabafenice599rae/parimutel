#!/usr/bin/env python3
"""Verifica profonda di uno stato dell'arena — un solo posto, tre chiamanti.

    python3 scripts/verify_state.py [--root DIR] [--json]

`rebuild_balances.py` risponde a "la proiezione combacia col ledger?".
Questo modulo risponde alla domanda piu' larga: **questo stato e' sano?**,
cioe' tutto quello che si puo' controllare guardando solo i file, senza sapere
quali bet erano attese.

Lo usano lo stress test (banco di prova locale), il test di accettazione
(GitHub vero) e la CI. Avere una definizione sola evita che le tre strade
divergano proprio su cosa significa "corretto".
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c
import rebuild_balances


def deep_check(root: Path):
    """Ritorna (problemi, statistiche). Lista vuota = stato sano."""
    root = Path(root)
    problems = []

    entries = c.read_ledger(root / "ledger")
    balances = c.load_json(root / "balances.json", c.default_balances())["balances"]
    events = c.load_json(root / "events.json", c.default_events())["events"]
    settled = c.load_json(root / "settled.json", c.default_settled())["settlements"]
    receipts = c.load_json(root / "receipts.json", c.default_receipts())["receipts"]
    state = c.load_json(root / "ledger/state.json", c.default_ledger_state())

    # --- I1 / I2 / I6 / I4: dallo stesso strumento della produzione
    report = rebuild_balances.audit(root)
    for key, label in (("chain", "I1 catena"), ("balances", "I2 saldi"),
                       ("state", "I6 indice"), ("negative", "I4 negativi")):
        problems += [f"{label}: {p}" for p in report[key]]

    # --- I6: ogni bet_id al massimo una BET_DEBIT
    applicate = {}
    for entry in entries:
        if entry["kind"] != c.KIND_BET_DEBIT:
            continue
        if entry["id"] in applicate:
            problems.append(
                f"I6 doppia spesa: {entry['id']} applicata a seq "
                f"{applicate[entry['id']]} e {entry['seq']}"
            )
        applicate[entry["id"]] = entry["seq"]

    # --- I5: nonce strettamente crescente per utente, nell'ordine del ledger
    ultimo = {}
    for entry in entries:
        if entry["kind"] != c.KIND_BET_DEBIT:
            continue
        user_id, nonce = entry["user_id"], entry["nonce"]
        if nonce <= ultimo.get(user_id, 0):
            problems.append(
                f"I5 nonce non monotono per {user_id}: {nonce} dopo "
                f"{ultimo[user_id]} (seq {entry['seq']})"
            )
        ultimo[user_id] = nonce

    # --- I4 nel tempo: il saldo non deve passare per un negativo nemmeno
    # transitoriamente, altrimenti c'e' stato un istante di scoperto
    corrente = {}
    for entry in entries:
        row = corrente.setdefault(entry["user_id"], {"available": 0, "at_risk": 0})
        if entry["kind"] == c.KIND_CREDIT:
            row["available"] += entry["amount"]
        elif entry["kind"] == c.KIND_BET_DEBIT:
            row["available"] -= entry["amount"]
            row["at_risk"] += entry["amount"]
        else:
            row["available"] += entry["amount"]
            row["at_risk"] -= int((entry.get("meta") or {}).get("stake", 0))
        if row["available"] < 0 or row["at_risk"] < 0:
            problems.append(
                f"I4 saldo negativo transitorio per {entry['user_id']} "
                f"a seq {entry['seq']}: {row}"
            )

    # --- I8: la cache dei pool combacia col ledger
    pools = {}
    for entry in entries:
        if entry["kind"] == c.KIND_BET_DEBIT:
            pool = pools.setdefault(entry["event_id"], {"yes": 0, "no": 0})
            pool[entry["side"]] += entry["amount"]
    for event_id, event in events.items():
        atteso = pools.get(event_id, {"yes": 0, "no": 0})
        if {k: int(v) for k, v in event.get("pool", {}).items()} != atteso:
            problems.append(
                f"I8 pool disallineato per {event_id}: "
                f"cache={event.get('pool')} ledger={atteso}"
            )

    # --- settlement: uno per evento, una SETTLE_USER per utente, conti in ordine
    per_utente = {}
    for entry in entries:
        if entry["kind"] == c.KIND_SETTLE_USER:
            chiave = (entry["event_id"], entry["user_id"])
            per_utente[chiave] = per_utente.get(chiave, 0) + 1
    for (event_id, user_id), quante in per_utente.items():
        if quante > 1:
            problems.append(
                f"doppio settlement: {quante} SETTLE_USER per {user_id} su {event_id}"
            )
    for event_id, rec in settled.items():
        if rec["payout_total"] + rec["takeout"] + rec["dust_to_house"] != rec["T"]:
            problems.append(f"I3 conservazione rotta su {event_id}: {rec}")
        if not rec["void"] and rec["payout_total"] > rec["distributable"]:
            problems.append(f"I3 payout oltre il distribuibile su {event_id}")
        evento = events.get(event_id)
        if evento and evento.get("state") != "SETTLED":
            problems.append(
                f"{event_id} e' in settled.json ma l'evento e' {evento.get('state')}"
            )
    for event_id, evento in events.items():
        if evento.get("state") == "SETTLED" and event_id not in settled:
            problems.append(f"{event_id} e' SETTLED ma manca da settled.json")

    # --- le ricevute non possono mentire al client
    for bet_id, seq in applicate.items():
        ricevuta = receipts.get(bet_id)
        if ricevuta is None:
            problems.append(f"bet applicata senza ricevuta: {bet_id}")
        elif ricevuta["status"] != "APPLIED":
            problems.append(
                f"ricevuta bugiarda: {bet_id} e' nel ledger (seq {seq}) ma la "
                f"ricevuta dice {ricevuta['status']} {ricevuta['code']}"
            )
    for bet_id, ricevuta in receipts.items():
        if ricevuta["status"] == "APPLIED" and bet_id not in applicate:
            problems.append(
                f"ricevuta bugiarda: {bet_id} risulta {ricevuta['code']} "
                "ma nel ledger non c'e'"
            )

    # --- conservazione globale: il denaro non si crea e non sparisce
    accreditato = sum(e["amount"] for e in entries if e["kind"] == c.KIND_CREDIT)
    nei_saldi = sum(r["available"] + r["at_risk"] for r in balances.values())
    casa = sum(r["house_total"] for r in settled.values())
    if accreditato != nei_saldi + casa:
        problems.append(
            f"conservazione globale rotta: accreditato={accreditato} "
            f"saldi={nei_saldi} casa={casa} "
            f"(delta={accreditato - nei_saldi - casa})"
        )

    stats = {
        "entries": len(entries),
        "bet_applicate": len(applicate),
        "utenti": len(balances),
        "eventi": len(events),
        "liquidati": len(settled),
        "ricevute": len(receipts),
        "accreditato": accreditato,
        "nei_saldi": nei_saldi,
        "casa": casa,
        "last_seq": state.get("last_seq", 0),
    }
    return problems, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verifica profonda dello stato")
    ap.add_argument("--root", default=None, help="directory dell'arena")
    ap.add_argument("--json", action="store_true", help="output machine-readable")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else c.root()
    problems, stats = deep_check(root)

    if args.json:
        print(json.dumps({"ok": not problems, "problemi": problems,
                          "stats": stats}, indent=2, sort_keys=True))
    else:
        print(f"entry={stats['entries']}  bet={stats['bet_applicate']}  "
              f"utenti={stats['utenti']}  eventi={stats['eventi']}  "
              f"liquidati={stats['liquidati']}")
        print(f"accreditato={stats['accreditato']}  saldi={stats['nei_saldi']}  "
              f"casa={stats['casa']}")
        if problems:
            print(f"\n{len(problems)} PROBLEMI")
            for p in problems:
                print(f"  - {p}")
        else:
            print("\nstato sano: catena, saldi, nonce, pool, idempotenza, "
                  "ricevute e conservazione")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
