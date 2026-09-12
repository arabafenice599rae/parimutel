#!/usr/bin/env python3
"""Test di accettazione pre-produzione: GitHub vero, workflow veri.

Lo stress test prova la logica contro un bare repo locale. Questo prova
l'integrazione: issue vere, drain veri, runner veri, e alla fine verifica
ledger -> saldi -> pool -> payout -> conservazione.

    export GITHUB_TOKEN=<pat con issues:write>
    export ARENA_SECRETS_FILE=~/.arena/test-secrets.json

    python3 scripts/acceptance_test.py --repo owner/arena --plan            # 1. cosa cliccare
    python3 scripts/acceptance_test.py --repo owner/arena --bets            # 2. apre le issue
    python3 scripts/acceptance_test.py --repo owner/arena --verify          # 3. verifica
    python3 scripts/acceptance_test.py --repo owner/arena --verify-settled  # 4. dopo i settle

Le fasi sono separate perche' due passaggi richiedono un umano: aprire gli
eventi/accreditare e liquidare passano da `workflow_dispatch`, che un token
d'integrazione non puo' innescare. Lo script dice esattamente cosa cliccare.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common as c
import verify_state

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"


def log(msg=""):
    print(msg, flush=True)


def api(method: str, path: str, payload=None, token: str = None):
    token = token or os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("serve GITHUB_TOKEN (con issues:write)")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "arena-acceptance")
    delay = 2
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                body = resp.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code < 500 and exc.code != 429:
                raise SystemExit(f"{method} {path} -> {exc.code}: {detail}")
        except urllib.error.URLError:
            pass
        if attempt < 3:
            time.sleep(delay)
            delay *= 2
    raise SystemExit(f"{method} {path}: fallita dopo 4 tentativi")


# --------------------------------------------------------------------------
# Il piano: chi punta cosa, e cosa deve succedere
# --------------------------------------------------------------------------

def build_plan(args, secrets: dict) -> dict:
    rng = random.Random(args.seed)
    users = sorted(secrets)[: args.users]
    if len(users) < 2:
        raise SystemExit("servono almeno 2 utenti nel magazzino segreti")
    events = [f"{args.event_prefix}{i}" for i in range(args.events)]

    budget = args.credit // (args.bets // len(users) + 2)
    issues, attese, ostili = [], {}, {}

    def firma(user, event, side, amount, nonce, con=None):
        bet = {"bet_id": f"{user}-{nonce}", "user_id": user, "event_id": event,
               "side": side, "amount": int(amount), "nonce": int(nonce),
               "ts": c.now_iso()}
        bet["sig"] = c.sign_bet(secrets[con or user], bet)
        return bet

    # Le bet di un utente vanno in coda in ordine di nonce: su GitHub il numero
    # di issue e' monotono, quindi un utente non puo' sorpassare se stesso.
    flussi = []
    per_utente = max(1, args.bets // len(users))
    for user in users:
        flusso = []
        for nonce in range(1, per_utente + 1):
            bet = firma(user, rng.choice(events), rng.choice(["yes", "no"]),
                        rng.randint(args.min_bet, budget), nonce)
            attese[bet["bet_id"]] = bet
            flusso.append(bet)
            if rng.random() < 0.2:          # consegna doppia: deve dare DUP
                flusso.append(bet)
        flussi.append(flusso)
    while any(flussi):
        flusso = rng.choice([f for f in flussi if f])
        issues.append(flusso.pop(0))

    # --- bet ostili, ognuna col controllo che DEVE fermarla
    vittima, attaccante = users[0], users[1]
    alto = per_utente + 50

    forgiata = firma(vittima, events[0], "yes", args.min_bet, alto,
                     con=attaccante)
    ostili[forgiata["bet_id"]] = ("firma forgiata da un altro utente", c.ERR_SIG)
    issues.append(forgiata)

    fantasma = {"bet_id": "u_fantasma-1", "user_id": "u_fantasma",
                "event_id": events[0], "side": "yes", "amount": args.min_bet,
                "nonce": 1, "ts": c.now_iso()}
    fantasma["sig"] = c.sign_bet("segreto-inventato", fantasma)
    ostili[fantasma["bet_id"]] = ("utente sconosciuto", c.ERR_UNKNOWN_USER)
    issues.append(fantasma)

    spiantato = firma(vittima, events[0], "yes", args.credit + 1, alto + 1)
    ostili[spiantato["bet_id"]] = ("saldo insufficiente", c.ERR_BALANCE)
    issues.append(spiantato)

    rng.shuffle(issues[-3:])
    return {"users": users, "events": events, "issues": issues,
            "attese": attese, "ostili": ostili, "credit": args.credit}


# --------------------------------------------------------------------------
# Fasi
# --------------------------------------------------------------------------

def fase_piano(plan, args):
    log("Cosa devi cliccare PRIMA (tab Actions -> Run workflow, branch main):\n")
    for event_id in plan["events"]:
        log(f"  create_event: event_id={event_id}  title=\"Accettazione {event_id}\"")
        log(f"                close_at={args.close_at}  takeout_bps={args.takeout_bps}")
        log(f"                min_bet={args.min_bet}  max_bet={args.credit * 100}")
    log("")
    for user in plan["users"]:
        log(f"  credit: user_id={user}  amount={plan['credit']}")
    log(f"\nPoi: python3 scripts/acceptance_test.py --repo {args.repo} --bets")
    log(f"     ({len(plan['issues'])} issue, di cui {len(plan['ostili'])} ostili)")


def fase_bets(plan, args):
    log(f"apro {len(plan['issues'])} issue in ondate da {args.burst} "
        "(le ondate servono a far accavallare i drain)")
    aperte = []
    for i, bet in enumerate(plan["issues"], 1):
        corpo = f"BET {bet['bet_id']}\n\n```json\n{json.dumps(bet, indent=2)}\n```\n"
        issue = api("POST", f"/repos/{args.repo}/issues",
                    {"title": f"BET {bet['bet_id']}", "body": corpo,
                     "labels": [args.label]})
        aperte.append(issue["number"])
        if i % args.burst == 0:
            log(f"  {i}/{len(plan['issues'])} (pausa {args.pause}s)")
            time.sleep(args.pause)
    log(f"aperte: #{min(aperte)}-#{max(aperte)}")
    return aperte


def leggi_stato_remoto(args, nome: str):
    url = f"{RAW}/{args.repo}/{args.branch}/{nome}?t={int(time.time())}"
    req = urllib.request.Request(url, headers={"User-Agent": "arena-acceptance",
                                               "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        return None


def attendi_ricevute(plan, args):
    """Aspetta che ogni bet inviata abbia una ricevuta. La verita' e' li'."""
    attesi = set(plan["attese"]) | set(plan["ostili"])
    scadenza = time.time() + args.timeout
    visti = set()
    while time.time() < scadenza:
        doc = leggi_stato_remoto(args, "receipts.json") or {"receipts": {}}
        visti = attesi & set(doc.get("receipts", {}))
        if visti == attesi:
            log(f"tutte le {len(attesi)} ricevute sono arrivate")
            return True
        log(f"  ricevute {len(visti)}/{len(attesi)}...")
        time.sleep(args.poll)
    mancanti = sorted(attesi - visti)
    log(f"TIMEOUT: mancano {len(mancanti)} ricevute, es. {mancanti[:5]}")
    return False


def payout_atteso(entries, event_id, outcome, takeout_bps, void_if_one_sided):
    """Ricalcolo INDIPENDENTE del parimutuel.

    Volutamente non importa `settle.compute_settlement`: verificare il codice
    con se stesso non prova niente. Qui la formula e' riscritta dalla spec.
    """
    stakes = {"yes": {}, "no": {}}
    for e in entries:
        if e["kind"] == c.KIND_BET_DEBIT and e["event_id"] == event_id:
            stakes[e["side"]][e["user_id"]] = \
                stakes[e["side"]].get(e["user_id"], 0) + e["amount"]
    s_yes = sum(stakes["yes"].values())
    s_no = sum(stakes["no"].values())
    totale = s_yes + s_no
    utenti = sorted(set(stakes["yes"]) | set(stakes["no"]))
    puntato = {u: stakes["yes"].get(u, 0) + stakes["no"].get(u, 0) for u in utenti}

    rimborso = (outcome == "void" or (outcome in c.SIDES and not stakes[outcome])
                or (void_if_one_sided and (s_yes == 0 or s_no == 0)))
    if rimborso:
        return {u: puntato[u] for u in utenti}, 0, totale

    takeout = totale * takeout_bps // 10000
    distribuibile = totale - takeout
    vincente = s_yes if outcome == "yes" else s_no
    payout = {u: stakes[outcome].get(u, 0) * distribuibile // vincente
              for u in utenti}
    dust = distribuibile - sum(payout.values())
    return payout, takeout + dust, totale


def fase_verifica(plan, args, attesa_liquidazione: bool):
    tmp = Path(tempfile.mkdtemp(prefix="arena-accept-"))
    try:
        subprocess.run(["git", "clone", "-q", "--depth", "50",
                        f"https://github.com/{args.repo}.git", str(tmp / "repo")],
                       check=True)
        root = tmp / "repo"
        problemi, stats = verify_state.deep_check(root)

        entries = c.read_ledger(root / "ledger")
        receipts = c.load_json(root / "receipts.json")["receipts"]
        events = c.load_json(root / "events.json")["events"]
        settled = c.load_json(root / "settled.json")["settlements"]
        applicate = {e["id"] for e in entries if e["kind"] == c.KIND_BET_DEBIT}

        for bet_id, bet in plan["attese"].items():
            if bet_id not in applicate:
                r = receipts.get(bet_id)
                problemi.append(f"bet valida non applicata: {bet_id} "
                                f"(ricevuta: {r['code'] if r else 'ASSENTE'})")
        for bet_id, (perche, atteso) in plan["ostili"].items():
            if bet_id in applicate:
                problemi.append(f"bet ostile applicata ({perche}): {bet_id}")
            r = receipts.get(bet_id)
            if r and r["code"] != atteso:
                problemi.append(f"bet ostile {bet_id} ({perche}): atteso "
                                f"{atteso}, ottenuto {r['code']}")

        # pool attesi dalle sole bet che DOVEVANO entrare
        attesi = {}
        for bet in plan["attese"].values():
            pool = attesi.setdefault(bet["event_id"], {"yes": 0, "no": 0})
            pool[bet["side"]] += bet["amount"]
        for event_id, pool in attesi.items():
            reale = events.get(event_id, {}).get("pool")
            if reale and {k: int(v) for k, v in reale.items()} != pool:
                problemi.append(f"pool di {event_id}: atteso {pool}, trovato {reale}")

        if attesa_liquidazione:
            for event_id in plan["events"]:
                rec = settled.get(event_id)
                if rec is None:
                    problemi.append(f"{event_id} non risulta liquidato")
                    continue
                evento = events[event_id]
                payout, casa, totale = payout_atteso(
                    entries, event_id, rec["outcome"],
                    int(evento["takeout_bps"]),
                    bool(evento.get("void_if_one_sided", True)))
                if rec["T"] != totale:
                    problemi.append(f"{event_id}: T={rec['T']}, ricalcolato {totale}")
                if rec["house_total"] != casa:
                    problemi.append(f"{event_id}: casa={rec['house_total']}, "
                                    f"ricalcolata {casa}")
                if rec["payout_total"] != sum(payout.values()):
                    problemi.append(f"{event_id}: payout_total={rec['payout_total']}, "
                                    f"ricalcolato {sum(payout.values())}")
                for e in entries:
                    if e["kind"] == c.KIND_SETTLE_USER and e["event_id"] == event_id:
                        atteso_u = payout.get(e["user_id"], 0)
                        if e["amount"] != atteso_u:
                            problemi.append(
                                f"{event_id}/{e['user_id']}: payout {e['amount']}, "
                                f"ricalcolato {atteso_u}")

        log("")
        log(f"  entry={stats['entries']}  bet={stats['bet_applicate']}  "
            f"utenti={stats['utenti']}  eventi={stats['eventi']}  "
            f"liquidati={stats['liquidati']}")
        log(f"  accreditato={stats['accreditato']}  saldi={stats['nei_saldi']}  "
            f"casa={stats['casa']}")
        if problemi:
            log(f"\n\033[31m{len(problemi)} VIOLAZIONI\033[0m")
            for p in problemi[:40]:
                log(f"  - {p}")
            return 1
        log("\n\033[32mNESSUNA VIOLAZIONE\033[0m: ledger, saldi, pool, payout "
            "e conservazione tornano.")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Test di accettazione su GitHub vero")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--branch", default="main")
    ap.add_argument("--label", default="bet")
    ap.add_argument("--users", type=int, default=4)
    ap.add_argument("--bets", type=int, default=24)
    ap.add_argument("--events", type=int, default=2)
    ap.add_argument("--event-prefix", default="ev_acc")
    ap.add_argument("--credit", type=int, default=10000)
    ap.add_argument("--min-bet", type=int, default=10)
    ap.add_argument("--takeout-bps", type=int, default=300)
    ap.add_argument("--close-at", default="", help="ISO UTC per create_event")
    ap.add_argument("--burst", type=int, default=6, help="issue per ondata")
    ap.add_argument("--pause", type=float, default=3.0, help="pausa fra ondate")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--poll", type=int, default=20)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--plan-file", default=None)
    ap.add_argument("--plan", action="store_true", help="stampa cosa cliccare")
    ap.add_argument("--bets-phase", "--bets-only", dest="fase_bets",
                    action="store_true", help="apre le issue")
    ap.add_argument("--verify", action="store_true", help="attende e verifica")
    ap.add_argument("--verify-settled", action="store_true",
                    help="verifica anche i payout (dopo i settle)")
    args = ap.parse_args(argv)

    if not args.repo:
        raise SystemExit("serve --repo owner/nome")
    plan_file = Path(args.plan_file or f"/tmp/arena-accept-{args.seed}.json")

    try:
        secrets = c.load_user_secrets()
    except c.SecretsError as exc:
        raise SystemExit(f"magazzino segreti non leggibile: {exc}")

    if args.plan or not plan_file.exists():
        plan = build_plan(args, secrets)
        plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        log(f"piano salvato in {plan_file}\n")
        if args.plan:
            fase_piano(plan, args)
            return 0
    else:
        plan = json.loads(plan_file.read_text(encoding="utf-8"))

    if args.fase_bets:
        fase_bets(plan, args)
        log(f"\nora: python3 scripts/acceptance_test.py --repo {args.repo} --verify")
        return 0

    if args.verify or args.verify_settled:
        if not attendi_ricevute(plan, args) and not args.verify_settled:
            return 1
        return fase_verifica(plan, args, attesa_liquidazione=args.verify_settled)

    fase_piano(plan, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
