#!/usr/bin/env python3
"""Client Arena Parimutuel per a-Shell (solo stdlib).

    python3 arena.py events                  # eventi aperti + quote implicite
    python3 arena.py balance                 # il MIO saldo (verita': balances.json)
    python3 arena.py bet ev_derby yes 500    # firma, invia, aspetta la ricevuta
    python3 arena.py receipt u_ab12ef34-7    # ricontrolla una ricevuta
    python3 arena.py sign ev_derby yes 500   # solo firma, stampa il payload

Config in `~/Documents/.arena/config.json` (override: $ARENA_CONFIG):

    {"user_id":"u_ab12ef34","secret":"hex64","repo":"owner/arena",
     "branch":"main","read":"raw","channel":"issue","token":"<solo repo privato>"}

Il segreto non lascia MAI il telefono: viaggia solo l'HMAC. Il saldo locale non
e' mai la verita': comanda `balances.json`.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"
SIG_PREFIX = "v1"
API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


# --------------------------------------------------------------------------
# Config e stato locale
# --------------------------------------------------------------------------

def config_path() -> Path:
    env = os.environ.get("ARENA_CONFIG")
    if env:
        return Path(env).expanduser()
    preferred = Path.home() / "Documents" / ".arena" / "config.json"
    return preferred if preferred.exists() else Path.home() / ".arena" / "config.json"


def load_config() -> dict:
    path = config_path()
    if not path.exists():
        raise SystemExit(
            f"config non trovato in {path}\n"
            "Chiedi all'owner il tuo config.json e salvalo li' (§5.3)."
        )
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for field in ("user_id", "secret", "repo"):
        if not cfg.get(field):
            raise SystemExit(f"config incompleto: manca '{field}'")
    cfg.setdefault("branch", "main")
    cfg.setdefault("read", "raw")
    cfg.setdefault("channel", "issue")
    return cfg


def nonce_path() -> Path:
    return config_path().parent / "nonce.json"


def next_nonce(cfg: dict, remote_last: int) -> int:
    """max(nonce remoto, nonce locale) + 1.

    Il locale serve perche' fra l'invio e il drain il `last_nonce` remoto non e'
    ancora avanzato: senza memoria locale due bet ravvicinate userebbero lo
    stesso nonce e la seconda prenderebbe ERR_NONCE.
    """
    local = 0
    path = nonce_path()
    if path.exists():
        try:
            local = int(json.loads(path.read_text(encoding="utf-8")).get(cfg["user_id"], 0))
        except (ValueError, json.JSONDecodeError):
            local = 0
    return max(int(remote_last or 0), local) + 1


def remember_nonce(cfg: dict, nonce: int) -> None:
    path = nonce_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    data[cfg["user_id"]] = max(int(data.get(cfg["user_id"], 0)), int(nonce))
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Firma (deve combaciare con scripts/common.py — stringa canonica §3.6)
# --------------------------------------------------------------------------

def signing_string(bet: dict) -> str:
    return "|".join(
        [SIG_PREFIX, str(bet["bet_id"]), str(bet["user_id"]), str(bet["event_id"]),
         str(bet["side"]), str(bet["amount"]), str(bet["nonce"]), str(bet["ts"])]
    )


def sign_bet(secret: str, bet: dict) -> str:
    return hmac.new(
        secret.encode("utf-8"), signing_string(bet).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def compose_bet(cfg: dict, event_id: str, side: str, amount: int, nonce: int) -> dict:
    bet = {
        "bet_id": f"{cfg['user_id']}-{nonce}",
        "user_id": cfg["user_id"],
        "event_id": event_id,
        "side": side,
        "amount": int(amount),
        "nonce": int(nonce),
        "ts": datetime.now(timezone.utc).strftime(ISO_FMT),
    }
    bet["sig"] = sign_bet(cfg["secret"], bet)
    return bet


# --------------------------------------------------------------------------
# Rete
# --------------------------------------------------------------------------

def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise SystemExit(f"HTTP {exc.code} su {req.full_url}\n{detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"rete non raggiungibile: {exc}")


def read_state(cfg: dict, name: str):
    """Legge un file di stato dal repo. Tier 0 -> raw, Tier 1/2 -> API."""
    local = os.environ.get("ARENA_LOCAL_ROOT")
    if local:  # modalita' offline (test, uso sullo stesso repo clonato)
        path = Path(local) / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    if cfg["read"] == "raw" and not cfg.get("token"):
        url = f"{RAW}/{cfg['repo']}/{cfg['branch']}/{name}?t={int(time.time())}"
        req = urllib.request.Request(url)
    else:
        query = urllib.parse.urlencode({"ref": cfg["branch"]})
        url = f"{API}/repos/{cfg['repo']}/contents/{name}?{query}"
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/vnd.github.raw+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "arena-client")
    req.add_header("Cache-Control", "no-cache")
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    return json.loads(_open(req))


def post(cfg: dict, path: str, payload: dict):
    token = cfg.get("token") or os.environ.get("ARENA_TOKEN")
    if not token:
        raise SystemExit(
            "serve un token per inviare la bet: mettilo in config.json "
            "('token') oppure in $ARENA_TOKEN (fine-grained, solo questo repo, "
            "issues:write)"
        )
    req = urllib.request.Request(
        f"{API}{path}", data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "arena-client")
    body = _open(req)
    return json.loads(body) if body else {}


def submit(cfg: dict, bet: dict):
    """Invia la bet sul canale configurato. Ritorna una descrizione breve."""
    body = f"BET {bet['bet_id']}\n\n```json\n{json.dumps(bet, indent=2)}\n```\n"
    if cfg["channel"] == "issue":
        issue = post(
            cfg,
            f"/repos/{cfg['repo']}/issues",
            {"title": f"BET {bet['bet_id']}", "body": body, "labels": ["bet"]},
        )
        return f"issue #{issue.get('number')}"
    if cfg["channel"] == "dispatch":
        post(
            cfg,
            f"/repos/{cfg['repo']}/actions/workflows/place_bet.yml/dispatches",
            {"ref": cfg["branch"], "inputs": {"bet": json.dumps(bet)}},
        )
        return "workflow_dispatch place_bet"
    raise SystemExit(f"channel sconosciuto: {cfg['channel']} (usa issue o dispatch)")


# --------------------------------------------------------------------------
# Viste
# --------------------------------------------------------------------------

def multiplier(pool: dict, side: str, takeout_bps: int):
    s = int(pool.get(side, 0))
    total = int(pool.get("yes", 0)) + int(pool.get("no", 0))
    if s <= 0 or total <= 0:
        return None
    return (1 - takeout_bps / 10000) * total / s


def fmt_mult(value):
    return f"{value:.2f}x" if value else "  -  "


def my_row(cfg: dict):
    balances = (read_state(cfg, "balances.json") or {}).get("balances", {})
    return balances.get(cfg["user_id"], {
        "available": 0, "at_risk": 0, "lifetime_credited": 0, "last_nonce": 0
    })


def cmd_events(cfg, args):
    doc = read_state(cfg, "events.json") or {"events": {}}
    rows = sorted(doc["events"].values(), key=lambda e: (e["state"] != "OPEN", e["close_at"]))
    if not rows:
        print("nessun evento")
        return 0
    for ev in rows:
        if ev["state"] != "OPEN" and not args.all:
            continue
        pool = ev.get("pool", {})
        total = int(pool.get("yes", 0)) + int(pool.get("no", 0))
        print(f"\n{ev['id']}  [{ev['state']}]  {ev['title']}")
        print(f"  chiude: {ev['close_at']}   takeout: {ev['takeout_bps']/100:.2f}%"
              f"   limiti: {ev.get('min_bet')}-{ev.get('max_bet')}")
        for side in ("yes", "no"):
            label = ev.get("side_labels", {}).get(side, side)
            stake = int(pool.get(side, 0))
            share = f"{100 * stake / total:5.1f}%" if total else "  n/d"
            print(f"  {side:>3} {label:<18} pool={stake:>10}  {share}  "
                  f"x{fmt_mult(multiplier(pool, side, ev['takeout_bps']))}")
        if ev.get("outcome"):
            print(f"  esito: {ev['outcome']}")
    return 0


def cmd_balance(cfg, args):
    row = my_row(cfg)
    print(f"utente:     {cfg['user_id']}")
    print(f"available:  {row['available']}")
    print(f"at_risk:    {row['at_risk']}")
    print(f"accreditato: {row['lifetime_credited']}")
    print(f"last_nonce: {row['last_nonce']}")
    return 0


def poll_receipt(cfg, bet_id: str, timeout: int, interval: int = 5):
    deadline = time.time() + timeout
    while True:
        doc = read_state(cfg, "receipts.json") or {"receipts": {}}
        receipt = doc.get("receipts", {}).get(bet_id)
        if receipt:
            return receipt
        if time.time() >= deadline:
            return None
        time.sleep(interval)


def show_receipt(bet_id: str, receipt):
    if receipt is None:
        print(f"nessuna ricevuta per {bet_id} (ancora in coda?): riprova con "
              f"`arena.py receipt {bet_id}`")
        return 2
    print(f"{receipt['status']} {receipt['code']}  ({receipt['ts']})")
    if receipt.get("reason"):
        print(f"  motivo:    {receipt['reason']}")
    if receipt.get("seq") is not None:
        print(f"  seq:       {receipt['seq']}")
    if receipt.get("available") is not None:
        print(f"  available: {receipt['available']}")
    return 0 if receipt["status"] == "APPLIED" else 1


def cmd_bet(cfg, args):
    if args.side not in ("yes", "no"):
        raise SystemExit("side deve essere 'yes' o 'no'")
    if args.amount <= 0:
        raise SystemExit("amount deve essere > 0")

    events = (read_state(cfg, "events.json") or {"events": {}})["events"]
    event = events.get(args.event)
    if event is None:
        raise SystemExit(f"evento sconosciuto: {args.event}")
    if event["state"] != "OPEN":
        raise SystemExit(f"evento {args.event} non e' OPEN")

    row = my_row(cfg)
    if args.amount > row["available"]:
        raise SystemExit(f"saldo insufficiente: available={row['available']}")

    nonce = args.nonce or next_nonce(cfg, row["last_nonce"])
    bet = compose_bet(cfg, args.event, args.side, args.amount, nonce)

    pool = dict(event.get("pool", {}))
    pool[args.side] = int(pool.get(args.side, 0)) + args.amount
    est = multiplier(pool, args.side, event["takeout_bps"])
    print(f"{bet['bet_id']}: {args.amount} su '{args.side}' "
          f"({event.get('side_labels', {}).get(args.side, args.side)})")
    print(f"  moltiplicatore stimato ora: x{fmt_mult(est)} "
          "(cambia con le puntate successive)")
    if not args.yes_i_am_sure:
        answer = input("confermi? [s/N] ").strip().lower()
        if answer not in ("s", "si", "sì", "y", "yes"):
            print("annullata")
            return 1

    remember_nonce(cfg, nonce)      # prima dell'invio: un nonce bruciato costa
    where = submit(cfg, bet)        # meno di due bet con lo stesso nonce
    print(f"inviata via {where}; attendo la ricevuta...")
    return show_receipt(bet["bet_id"], poll_receipt(cfg, bet["bet_id"], args.timeout))


def cmd_receipt(cfg, args):
    return show_receipt(args.bet_id, poll_receipt(cfg, args.bet_id, args.timeout))


def cmd_sign(cfg, args):
    row = my_row(cfg) if not args.nonce else {"last_nonce": 0}
    nonce = args.nonce or next_nonce(cfg, row["last_nonce"])
    print(json.dumps(compose_bet(cfg, args.event, args.side, args.amount, nonce)))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Client Arena Parimutuel")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("events", help="elenca gli eventi e le quote")
    p.add_argument("--all", action="store_true", help="mostra anche i SETTLED")
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("balance", help="mostra il tuo saldo")
    p.set_defaults(func=cmd_balance)

    p = sub.add_parser("bet", help="firma e invia una scommessa")
    p.add_argument("event")
    p.add_argument("side", choices=["yes", "no"])
    p.add_argument("amount", type=int)
    p.add_argument("--nonce", type=int, help="forza il nonce")
    p.add_argument("-y", "--yes-i-am-sure", action="store_true", help="niente conferma")
    p.add_argument("--timeout", type=int, default=180, help="attesa ricevuta (s)")
    p.set_defaults(func=cmd_bet)

    p = sub.add_parser("receipt", help="controlla una ricevuta")
    p.add_argument("bet_id")
    p.add_argument("--timeout", type=int, default=0)
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("sign", help="firma senza inviare (stampa il payload)")
    p.add_argument("event")
    p.add_argument("side", choices=["yes", "no"])
    p.add_argument("amount", type=int)
    p.add_argument("--nonce", type=int)
    p.set_defaults(func=cmd_sign)

    args = ap.parse_args(argv)
    return args.func(load_config(), args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrotto")
        raise SystemExit(130)
