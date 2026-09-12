#!/usr/bin/env python3
"""Console amministrativa dell'Arena — per a-Shell, solo stdlib.

    python3 arena_admin.py            # menu
    python3 arena_admin.py conti      # direttamente la contabilita'
    python3 arena_admin.py verifica   # controlli di integrita'

**Questa console non fa i conti.** Scarica lo stato dal repo e poi chiama i
moduli del progetto — `common.py`, `settle.py`, `verify_state.py` — cioe' gli
stessi che girano dentro le GitHub Actions. Se le regole finanziarie
cambiassero, cambierebbero in un posto solo. Una seconda implementazione della
matematica dei payout, su un telefono, sarebbe il modo piu' rapido per
ritrovarsi due veritai diverse.

Le scritture (crea evento, accredita, chiudi, liquida) non avvengono qui:
partono come `workflow_dispatch`, quindi l'unico scrittore resta il token
delle Action (I9). La console valida e conferma, poi chiede alla Action di
fare il lavoro.

Config in `~/Documents/.arena/admin.json` — separato da quello degli utenti:

    {"repo":"owner/arena","branch":"main","token":"<PAT con actions:write>"}
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com"
MODULI = ("common.py", "rebuild_balances.py", "verify_state.py", "settle.py")
STATO = ("events.json", "balances.json", "settled.json", "receipts.json")


# --------------------------------------------------------------------------
# Percorsi e config
# --------------------------------------------------------------------------

def _scrivibile(directory: Path) -> bool:
    try:
        return directory.is_dir() and os.access(directory, os.W_OK)
    except OSError:
        return False


def base_dir() -> Path:
    """Su a-Shell l'unica cartella scrivibile e' ~/Documents (non la home)."""
    env = os.environ.get("ARENA_ADMIN_HOME")
    if env:
        return Path(env).expanduser()
    home = Path.home()
    for candidata in (home / "Documents" / ".arena", home / ".arena",
                      Path.cwd() / ".arena"):
        if candidata.exists() or _scrivibile(candidata.parent):
            return candidata
    return Path.cwd() / ".arena"


def config_path() -> Path:
    return base_dir() / "admin.json"


def cache_dir() -> Path:
    return base_dir() / "cache"


def carica_config() -> dict:
    percorso = config_path()
    if not percorso.exists():
        raise SystemExit(
            f"config admin non trovato in {percorso}\n"
            "Crealo con:  python3 arena_admin.py setup"
        )
    cfg = json.loads(percorso.read_text(encoding="utf-8"))
    cfg.setdefault("branch", "main")
    if not cfg.get("repo"):
        raise SystemExit("config incompleto: manca 'repo'")
    return cfg


# --------------------------------------------------------------------------
# Rete
# --------------------------------------------------------------------------

def richiesta(cfg, metodo: str, percorso: str, payload=None, raw=False):
    url = percorso if percorso.startswith("http") else f"{API}{percorso}"
    dati = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=dati, method=metodo)
    req.add_header("Accept", "application/vnd.github.raw+json" if raw
                   else "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "arena-admin")
    req.add_header("Cache-Control", "no-cache")
    if dati is not None:
        req.add_header("Content-Type", "application/json")
    if cfg.get("token"):
        req.add_header("Authorization", f"Bearer {cfg['token']}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            corpo = resp.read()
            if raw:
                return corpo
            return json.loads(corpo.decode()) if corpo else None
    except urllib.error.HTTPError as exc:
        dettaglio = exc.read().decode("utf-8", "replace")[:300]
        if exc.code == 403 and "actions" in percorso:
            raise SystemExit(
                "403 sul dispatch: il token non ha il permesso Actions.\n"
                "Serve un fine-grained token con 'Actions: Read and write' "
                "su questo repo."
            )
        raise SystemExit(f"HTTP {exc.code} su {url}\n{dettaglio}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"rete non raggiungibile: {exc}")


def scarica_file(cfg, percorso_repo: str, destinazione: Path):
    query = urllib.parse.urlencode({"ref": cfg["branch"]})
    corpo = richiesta(cfg, "GET",
                      f"/repos/{cfg['repo']}/contents/{percorso_repo}?{query}",
                      raw=True)
    destinazione.parent.mkdir(parents=True, exist_ok=True)
    destinazione.write_bytes(corpo)


def aggiorna_stato(cfg, silenzioso=False) -> Path:
    """Porta lo stato del repo in una cache locale, poi si lavora su quella."""
    cache = cache_dir()
    shutil.rmtree(cache, ignore_errors=True)
    (cache / "ledger").mkdir(parents=True, exist_ok=True)

    for nome in STATO:
        scarica_file(cfg, nome, cache / nome)
    scarica_file(cfg, "ledger/state.json", cache / "ledger" / "state.json")

    query = urllib.parse.urlencode({"ref": cfg["branch"]})
    elenco = richiesta(cfg, "GET",
                       f"/repos/{cfg['repo']}/contents/ledger?{query}") or []
    shard = [v["name"] for v in elenco if v["name"].endswith(".ndjson")]
    for nome in sorted(shard):
        scarica_file(cfg, f"ledger/{nome}", cache / "ledger" / nome)

    if not silenzioso:
        print(f"stato aggiornato ({len(shard)} shard di ledger)")
    return cache


# --------------------------------------------------------------------------
# I moduli del progetto: il motore contabile, non una sua copia
# --------------------------------------------------------------------------

def cartella_moduli() -> Path:
    """Dove sta il motore contabile.

    Sul telefono i moduli stanno accanto alla console (li porta `setup`); in un
    clone del repo stanno in `scripts/`. Cercarli in entrambi i posti rende la
    stessa console usabile dal telefono e dal portatile.
    """
    qui = Path(__file__).resolve().parent
    for candidata in (qui, qui.parent / "scripts"):
        if all((candidata / m).exists() for m in MODULI):
            return candidata
    return qui


def importa_moduli():
    qui = cartella_moduli()
    mancanti = [m for m in MODULI if not (qui / m).exists()]
    if mancanti:
        raise SystemExit(
            "mancano i moduli del progetto: " + ", ".join(mancanti) +
            "\nScaricali con:  python3 arena_admin.py setup"
        )
    sys.path.insert(0, str(qui))
    import common
    import settle
    import verify_state
    return common, settle, verify_state


# --------------------------------------------------------------------------
# Formattazione
# --------------------------------------------------------------------------

def punti(valore) -> str:
    """12345 -> '12.345' (le migliaia all'italiana, niente decimali)."""
    return f"{int(valore):,}".replace(",", ".")


def riga(titolo: str, larghezza: int = 46):
    print(f"\n{titolo}")
    print("─" * larghezza)


def tabella(intestazioni, righe, allinea_destra=()):
    if not righe:
        print("  (vuoto)")
        return
    colonne = list(zip(*([intestazioni] + [[str(c) for c in r] for r in righe])))
    larghezze = [max(len(c) for c in col) for col in colonne]
    def formatta(valori):
        pezzi = []
        for i, valore in enumerate(valori):
            pezzi.append(str(valore).rjust(larghezze[i]) if i in allinea_destra
                         else str(valore).ljust(larghezze[i]))
        return "  ".join(pezzi).rstrip()
    print("  " + formatta(intestazioni))
    for r in righe:
        print("  " + formatta(r))


# --------------------------------------------------------------------------
# Viste di lettura
# --------------------------------------------------------------------------

def leggi(cache: Path, nome: str, comune):
    return comune.load_json(cache / nome, {})


def conti_globali(cache: Path, comune):
    """Tutti i numeri aggregati, calcolati dal ledger via common.reduce_balances."""
    entries = comune.read_ledger(cache / "ledger")
    saldi, _, _ = comune.reduce_balances(entries)
    liquidazioni = leggi(cache, "settled.json", comune).get("settlements", {})

    emessi = sum(e["amount"] for e in entries
                 if e["kind"] == comune.KIND_CREDIT and e["amount"] > 0)
    rettifiche = sum(e["amount"] for e in entries
                     if e["kind"] == comune.KIND_CREDIT and e["amount"] < 0)
    in_gioco = sum(r["at_risk"] for r in saldi.values())
    disponibili = sum(r["available"] for r in saldi.values())
    casa = sum(s["house_total"] for s in liquidazioni.values())
    oggi = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    casa_oggi = sum(s["house_total"] for s in liquidazioni.values()
                    if str(s.get("settled_at", "")).startswith(oggi))
    return {
        "entries": entries, "saldi": saldi, "liquidazioni": liquidazioni,
        "emessi": emessi, "rettifiche": rettifiche, "in_gioco": in_gioco,
        "disponibili": disponibili, "casa": casa, "casa_oggi": casa_oggi,
        "quadra": emessi + rettifiche == disponibili + in_gioco + casa,
    }


def vista_dashboard(cache: Path, comune):
    conti = conti_globali(cache, comune)
    eventi = leggi(cache, "events.json", comune).get("events", {})
    aperti = [e for e in eventi.values() if e["state"] == "OPEN"]
    adesso = comune.now_iso()
    in_finestra = [e for e in aperti if adesso < e["close_at"]]

    riga("ARENA — DASHBOARD")
    print(f"  Token emessi        {punti(conti['emessi']):>12}")
    print(f"  In gioco            {punti(conti['in_gioco']):>12}")
    print(f"  Disponibili         {punti(conti['disponibili']):>12}")
    print(f"  Alla casa           {punti(conti['casa']):>12}")
    print(f"  {'quadra' if conti['quadra'] else 'NON QUADRA':>32}")
    print()
    print(f"  Utenti              {len(conti['saldi']):>12}")
    print(f"  Eventi aperti       {len(aperti):>12}"
          f"   (in finestra: {len(in_finestra)})")
    print(f"  Eventi liquidati    {len(conti['liquidazioni']):>12}")
    print(f"  Movimenti a ledger  {len(conti['entries']):>12}")
    if conti["casa_oggi"]:
        print(f"\n  Incassato oggi      {punti(conti['casa_oggi']):>12}")

    scadenze = sorted(in_finestra, key=lambda e: e["close_at"])[:3]
    if scadenze:
        riga("PROSSIME CHIUSURE")
        tabella(["evento", "chiude", "pool"],
                [[e["id"], e["close_at"],
                  punti(e["pool"]["yes"] + e["pool"]["no"])] for e in scadenze],
                allinea_destra={2})


def vista_eventi(cache: Path, comune, solo=None):
    eventi = leggi(cache, "events.json", comune).get("events", {})
    liquidazioni = leggi(cache, "settled.json", comune).get("settlements", {})
    adesso = comune.now_iso()
    righe = []
    for ev in sorted(eventi.values(), key=lambda e: e["close_at"], reverse=True):
        pool = ev.get("pool", {})
        totale = pool.get("yes", 0) + pool.get("no", 0)
        if ev["state"] == "SETTLED":
            stato = f"SETTLED/{ev.get('outcome')}"
        elif adesso >= ev["close_at"]:
            stato = "CHIUSO"
        else:
            stato = "APERTO"
        if solo and solo.upper() not in stato:
            continue
        rec = liquidazioni.get(ev["id"], {})
        righe.append([ev["id"], ev["title"][:24], stato, punti(totale),
                      punti(pool.get("yes", 0)), punti(pool.get("no", 0)),
                      punti(rec.get("house_total", totale * ev["takeout_bps"] // 10000))])
    riga("EVENTI", 72)
    tabella(["id", "titolo", "stato", "pool", "yes", "no", "casa"],
            righe, allinea_destra={3, 4, 5, 6})


def vista_utenti(cache: Path, comune):
    conti = conti_globali(cache, comune)
    righe = [[u, punti(r["available"]), punti(r["at_risk"]),
              punti(r["available"] + r["at_risk"]), punti(r["lifetime_credited"])]
             for u, r in sorted(conti["saldi"].items())]
    riga("UTENTI", 64)
    tabella(["id", "saldo", "in gioco", "totale", "accreditato"],
            righe, allinea_destra={1, 2, 3, 4})


def vista_utente(cache: Path, comune, user_id: str):
    user_id = comune.clean_id(user_id)
    entries = comune.read_ledger(cache / "ledger")
    saldi, _, _ = comune.reduce_balances(entries)
    r = saldi.get(user_id)
    if r is None:
        print(f"  {user_id}: nessun movimento a ledger")
        return
    riga(f"UTENTE {user_id}", 52)
    print(f"  Saldo disponibile   {punti(r['available']):>12}")
    print(f"  In gioco            {punti(r['at_risk']):>12}")
    print(f"  Totale              {punti(r['available'] + r['at_risk']):>12}")
    print(f"  Accreditato (vita)  {punti(r['lifetime_credited']):>12}")

    righe = []
    for e in entries:
        if e["user_id"] != user_id:
            continue
        if e["kind"] == comune.KIND_BET_DEBIT:
            che = f"BET {e['event_id']} {e['side']}"
            importo = -e["amount"]
        elif e["kind"] == comune.KIND_SETTLE_USER:
            che = f"SETTLE {e['event_id']} (stake {punti(e['meta']['stake'])})"
            importo = e["amount"]
        else:
            che = "CREDIT" + (f" — {e['meta']['reason']}"
                              if (e.get("meta") or {}).get("reason") else "")
            importo = e["amount"]
        righe.append([e["seq"], e["ts"][:16].replace("T", " "),
                      f"{importo:+}".replace("-", "−"), che])
    riga("MOVIMENTI", 64)
    tabella(["seq", "quando", "importo", "che cosa"], righe, allinea_destra={0, 2})


def vista_ledger(cache: Path, comune, quanti=25):
    entries = comune.read_ledger(cache / "ledger")
    riga(f"LEDGER — ultime {min(quanti, len(entries))} di {len(entries)}", 76)
    # `amount` e' il campo della entry, non l'effetto sul disponibile: una
    # BET_DEBIT lo registra positivo perche' descrive quanto si sposta da
    # available a at_risk. La vista resta fedele al file, la legenda spiega.
    print("  amount = campo della entry · BET_DEBIT sposta da disponibile a "
          "in gioco")
    righe = [[e["seq"], e["ts"][:16].replace("T", " "), e["kind"],
              e["user_id"], f"{e['amount']:+}".replace("-", "−"),
              e.get("event_id") or "", e["hash"][:8]]
             for e in entries[-quanti:]]
    tabella(["seq", "quando", "tipo", "utente", "amount", "evento", "hash"],
            righe, allinea_destra={0, 4})


def vista_liquidazioni(cache: Path, comune):
    liquidazioni = leggi(cache, "settled.json", comune).get("settlements", {})
    righe = [[s["event_id"], s["outcome"], "si" if s["void"] else "no",
              punti(s["T"]), punti(s["takeout"]), punti(s["dust_to_house"]),
              punti(s["payout_total"]), punti(s["house_total"]),
              s["settled_at"][:16].replace("T", " ")]
             for s in sorted(liquidazioni.values(), key=lambda s: s["settled_at"])]
    riga("SETTLEMENT", 86)
    tabella(["evento", "esito", "void", "T", "takeout", "dust", "payout", "casa",
             "quando"], righe, allinea_destra={3, 4, 5, 6, 7})


def vista_verifica(cache: Path, verifica_stato):
    riga("CONTROLLI DI INTEGRITA'", 52)
    problemi, stats = verifica_stato.deep_check(cache)
    print(f"  entry={stats['entries']}  bet={stats['bet_applicate']}  "
          f"utenti={stats['utenti']}  eventi={stats['eventi']}")
    print(f"  accreditato={punti(stats['accreditato'])}  "
          f"saldi={punti(stats['nei_saldi'])}  casa={punti(stats['casa'])}")
    if problemi:
        print(f"\n  {len(problemi)} PROBLEMI")
        for p in problemi[:25]:
            print(f"   - {p}")
    else:
        print("\n  Tutto in ordine: catena, saldi ricostruiti, nonce, pool,")
        print("  idempotenza, ricevute e conservazione globale.")
    return not problemi


def anteprima_settlement(cache: Path, comune, liquida, event_id: str, esito: str):
    """Chi prende quanto — calcolato dagli stessi moduli del server."""
    eventi = leggi(cache, "events.json", comune).get("events", {})
    evento = eventi.get(event_id)
    if evento is None:
        print(f"  evento sconosciuto: {event_id}")
        return None
    puntate, totali = liquida.collect_stakes(cache / "ledger", event_id)
    try:
        esito_calcolato = liquida.compute_settlement(
            puntate, totali, esito, int(evento["takeout_bps"]),
            bool(evento.get("void_if_one_sided", True)))
    except liquida.Abort as exc:
        print(f"  il settlement si rifiuterebbe: {exc}")
        return None
    riga(f"ANTEPRIMA — {event_id} con esito '{esito}'", 52)
    print(f"  pool yes={punti(totali['yes'])}  no={punti(totali['no'])}  "
          f"T={punti(esito_calcolato['T'])}")
    if esito_calcolato["void"]:
        print("  RIMBORSO TOTALE (void o mercato monolaterale): nessun takeout")
    else:
        print(f"  takeout={punti(esito_calcolato['takeout'])}  "
              f"dust={punti(esito_calcolato['dust_to_house'])}  "
              f"alla casa={punti(esito_calcolato['house_total'])}")
    tabella(["utente", "puntato", "incassa"],
            [[u, punti(esito_calcolato["stakes"][u]),
              punti(esito_calcolato["payouts"][u])]
             for u in esito_calcolato["users"]], allinea_destra={1, 2})
    return esito_calcolato


# --------------------------------------------------------------------------
# Scritture: sempre via workflow_dispatch (I9)
# --------------------------------------------------------------------------

def dispatch(cfg, workflow: str, inputs: dict):
    if not cfg.get("token"):
        raise SystemExit("serve un token con 'Actions: Read and write' nel config")
    richiesta(cfg, "POST",
              f"/repos/{cfg['repo']}/actions/workflows/{workflow}/dispatches",
              {"ref": cfg["branch"], "inputs": {k: str(v) for k, v in inputs.items()}})
    print(f"\n  richiesto: {workflow}")
    print(f"  segui il run su https://github.com/{cfg['repo']}/actions")


def conferma(domanda: str) -> bool:
    risposta = input(f"{domanda} [s/N] ").strip().lower()
    print()
    return risposta in ("s", "si", "sì", "y", "yes")


def chiedi(etichetta: str, default=None, obbligatorio=True) -> str:
    suffisso = f" [{default}]" if default else ""
    while True:
        valore = input(f"{etichetta}{suffisso}: ").strip() or (default or "")
        print()
        if valore or not obbligatorio:
            return valore
        print("  serve un valore")


def azione_crea_evento(cfg):
    event_id = chiedi("event_id (es. ev_derby)")
    titolo = chiedi("titolo")
    close_at = chiedi("chiusura UTC (2026-09-20T18:00:00Z)")
    yes_label = chiedi("etichetta lato YES", "Yes")
    no_label = chiedi("etichetta lato NO", "No")
    takeout = chiedi("takeout in bps (300 = 3%)", "300")
    min_bet = chiedi("puntata minima", "100")
    max_bet = chiedi("puntata massima", "1000000")
    print(f"  {event_id}: \"{titolo}\"  chiude {close_at}")
    print(f"  takeout {int(takeout) / 100:.2f}%   limiti [{min_bet}, {max_bet}]")
    if conferma("Creo l'evento?"):
        dispatch(cfg, "create_event.yml", {
            "event_id": event_id, "title": titolo, "close_at": close_at,
            "yes_label": yes_label, "no_label": no_label,
            "takeout_bps": takeout, "min_bet": min_bet, "max_bet": max_bet})


def azione_accredita(cfg, cache, comune):
    vista_utenti(cache, comune)
    user_id = chiedi("\nuser_id destinatario")
    importo = chiedi("punti (negativo = rettifica)")
    motivo = chiedi("nota operativa", obbligatorio=False)
    saldi, _, _ = comune.reduce_balances(comune.read_ledger(cache / "ledger"))
    attuale = saldi.get(user_id, {}).get("available", 0)
    print(f"  {user_id}: {punti(attuale)} → {punti(attuale + int(importo))}")
    if conferma("Accredito?"):
        dispatch(cfg, "credit.yml", {
            "user_id": user_id, "amount": importo, "reason": motivo,
            "credit_id": ""})


def azione_chiudi(cfg, cache, comune):
    vista_eventi(cache, comune, solo="APERTO")
    event_id = chiedi("\nevent_id da chiudere subito")
    print("  Le bet successive prenderanno ERR_EVENT_CLOSED.")
    if conferma("Chiudo la finestra?"):
        dispatch(cfg, "close_event.yml", {"event_id": event_id})


def azione_liquida(cfg, cache, comune, liquida):
    vista_eventi(cache, comune)
    event_id = chiedi("\nevent_id da liquidare")
    esito = chiedi("esito (yes / no / void)")
    if esito not in ("yes", "no", "void"):
        print("  esito non valido")
        return
    if anteprima_settlement(cache, comune, liquida, event_id, esito) is None:
        return
    eventi = leggi(cache, "events.json", comune).get("events", {})
    serve_force = comune.now_iso() < eventi[event_id]["close_at"] and esito != "void"
    if serve_force:
        print("  ATTENZIONE: la finestra e' ancora aperta, serve forzare.")
    print("  Il settlement e' terminale: non esiste un annulla.")
    if conferma(f"Liquido {event_id} con esito '{esito}'?"):
        dispatch(cfg, "settle.yml", {
            "event_id": event_id, "outcome": esito,
            "force": "true" if serve_force else "false", "dry_run": "false"})


# --------------------------------------------------------------------------
# Setup e menu
# --------------------------------------------------------------------------

def sembra_un_token(valore: str) -> bool:
    """Scarta le sciocchezze evidenti (su a-Shell l'output di un comando
    precedente finisce facilmente dentro il prompt). Non valida il token:
    quello lo puo' fare solo GitHub."""
    valore = valore.strip()
    if valore.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")):
        return True
    return len(valore) >= 36 and all(ch.isalnum() or ch in "_-" for ch in valore)


def azione_setup(args):
    percorso = config_path()
    if percorso.exists() and not args.force:
        raise SystemExit(f"{percorso} esiste gia' (usa --force)")
    repo = args.repo or chiedi("repo (owner/nome)")
    branch = args.branch or "main"
    token = args.token if args.token is not None else chiedi(
        "token con Actions+Contents (invio per sola lettura)", obbligatorio=False)
    if token and not sembra_un_token(token):
        print(f"  '{token[:16]}' non sembra un token GitHub: lo ignoro.")
        print("  Aggiungilo poi con: python3 arena_admin.py setup --force")
        token = ""
    cfg = {"repo": repo, "branch": branch}
    if token:
        cfg["token"] = token
    percorso.parent.mkdir(parents=True, exist_ok=True)
    percorso.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    try:
        os.chmod(percorso, 0o600)
    except OSError:
        pass
    print(f"scritto {percorso}")
    if token:
        print("  ATTENZIONE: questo token puo' lanciare i workflow, quindi\n"
              "  accreditare e liquidare. Non va sul telefono di un utente.")
    else:
        print("  Senza token: sola lettura. Le scritture chiederanno un token.")

    qui = Path(__file__).resolve().parent
    print("scarico i moduli del progetto (il motore contabile, non una copia):")
    for modulo in MODULI:
        scarica_file(cfg, f"scripts/{modulo}", qui / modulo)
        print(f"  {modulo}")
    print("\nPronto: python3 arena_admin.py")
    return 0


MENU = """
╔══════════════════════════════════════╗
║            ARENA ADMIN               ║
╠══════════════════════════════════════╣
║  1  Dashboard                        ║
║  2  Eventi                           ║
║  3  Utenti                           ║
║  4  Scheda utente                    ║
║  5  Contabilita'                     ║
║  6  Ledger                           ║
║  7  Settlement                       ║
║  8  Controlli integrita'             ║
╠══════════════════════════════════════╣
║  9  Crea evento                      ║
║ 10  Accredita token                  ║
║ 11  Chiudi evento                    ║
║ 12  Risolvi evento                   ║
╠══════════════════════════════════════╣
║  a  Aggiorna dati    0  Esci         ║
╚══════════════════════════════════════╝"""


def menu(cfg, cache, comune, liquida, verifica_stato):
    while True:
        print(MENU)
        scelta = input("scelta: ").strip().lower()
        try:
            if scelta == "0":
                return 0
            elif scelta == "a":
                cache = aggiorna_stato(cfg)
            elif scelta == "1":
                vista_dashboard(cache, comune)
            elif scelta == "2":
                vista_eventi(cache, comune)
            elif scelta == "3":
                vista_utenti(cache, comune)
            elif scelta == "4":
                vista_utente(cache, comune, chiedi("user_id"))
            elif scelta == "5":
                vista_contabilita(cache, comune)
            elif scelta == "6":
                vista_ledger(cache, comune)
            elif scelta == "7":
                vista_liquidazioni(cache, comune)
            elif scelta == "8":
                vista_verifica(cache, verifica_stato)
            elif scelta == "9":
                azione_crea_evento(cfg)
            elif scelta == "10":
                azione_accredita(cfg, cache, comune)
            elif scelta == "11":
                azione_chiudi(cfg, cache, comune)
            elif scelta == "12":
                azione_liquida(cfg, cache, comune, liquida)
            else:
                print("  scelta non valida")
        except SystemExit as exc:
            print(f"  {exc}")
        except (EOFError, KeyboardInterrupt):
            return 0


def vista_contabilita(cache: Path, comune):
    conti = conti_globali(cache, comune)
    riga("ARENA — CONTABILITA'")
    print(f"  Token emessi        {punti(conti['emessi']):>12}")
    if conti["rettifiche"]:
        print(f"  Rettifiche          {punti(conti['rettifiche']):>12}")
    print(f"  Token in gioco      {punti(conti['in_gioco']):>12}")
    print(f"  Token disponibili   {punti(conti['disponibili']):>12}")
    print(f"  Token alla casa     {punti(conti['casa']):>12}")
    if conti["casa_oggi"]:
        print(f"  di cui oggi         {punti(conti['casa_oggi']):>12}")
    print()
    somma = conti["disponibili"] + conti["in_gioco"] + conti["casa"]
    print(f"  emessi + rettifiche = {punti(conti['emessi'] + conti['rettifiche'])}")
    print(f"  disponibili + in gioco + casa = {punti(somma)}")
    print(f"  {'I conti quadrano.' if conti['quadra'] else 'NON QUADRA: lancia i controlli di integrita'}")
    vista_eventi(cache, comune)
    vista_utenti(cache, comune)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Console amministrativa dell'Arena")
    ap.add_argument("comando", nargs="?", default="menu",
                    choices=["menu", "setup", "dashboard", "eventi", "utenti",
                             "utente", "conti", "ledger", "settlement",
                             "verifica", "anteprima"])
    ap.add_argument("argomento", nargs="?", help="user_id o event_id")
    ap.add_argument("esito", nargs="?", help="per anteprima: yes/no/void")
    ap.add_argument("--repo"), ap.add_argument("--branch", default="main")
    ap.add_argument("--token", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="usa la cache locale senza riscaricare")
    args = ap.parse_args(argv)

    if args.comando == "setup":
        return azione_setup(args)

    cfg = carica_config()
    comune, liquida, verifica_stato = importa_moduli()
    cache = cache_dir() if args.offline else aggiorna_stato(cfg, silenzioso=True)
    if not (cache / "ledger").exists():
        raise SystemExit("cache assente: lancia senza --offline")

    if args.comando == "menu":
        return menu(cfg, cache, comune, liquida, verifica_stato)
    if args.comando == "dashboard":
        vista_dashboard(cache, comune)
    elif args.comando == "eventi":
        vista_eventi(cache, comune, solo=args.argomento)
    elif args.comando == "utenti":
        vista_utenti(cache, comune)
    elif args.comando == "utente":
        vista_utente(cache, comune, args.argomento or chiedi("user_id"))
    elif args.comando == "conti":
        vista_contabilita(cache, comune)
    elif args.comando == "ledger":
        vista_ledger(cache, comune, int(args.argomento or 25))
    elif args.comando == "settlement":
        vista_liquidazioni(cache, comune)
    elif args.comando == "anteprima":
        anteprima_settlement(cache, comune, liquida,
                             args.argomento or chiedi("event_id"),
                             args.esito or chiedi("esito"))
    elif args.comando == "verifica":
        return 0 if vista_verifica(cache, verifica_stato) else 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrotto")
        raise SystemExit(130)
