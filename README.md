# Arena Parimutuel

Piattaforma di scommesse **parimutuel** multiutente e **serverless** su eventi
binari (`yes`/`no`), con spread implicito = takeout. Lo stato vive in questo
repository; le **GitHub Actions sono il backend**. Il client gira in a-Shell su
iPhone e usa solo la stdlib Python.

Non e' una blockchain (c'e' un owner fidato che accredita e risolve) e non
custodisce denaro reale: i saldi sono punti interni.

---

## Mappa del repository

| File | Ruolo |
|---|---|
| `ledger/<YYYY-MM>.ndjson` | **fonte di verita'**: append-only, hash-chained |
| `ledger/state.json` | indice operativo (`last_seq`, `head_hash`, `applied_bets`) |
| `balances.json` | proiezione dei saldi (cache ricostruibile) |
| `events.json` | eventi, finestre, takeout, pool in cache |
| `settled.json` | record idempotente di ogni liquidazione |
| `receipts.json` | esito per `bet_id` — **e' qui che il client fa polling** |
| `secrets.age` | segreti utente cifrati (opzionale, §5.2 della spec) |
| `scripts/` | tutta la logica; gira solo dentro le Actions |
| `client/arena.py` | client a-Shell (solo stdlib) |
| `tests/`, `scripts/test_smoke.sh` | suite di conformita' I1–I10 e smoke end-to-end |

### Script

| Script | Cosa fa |
|---|---|
| `common.py` | hashing, catena, HMAC, reducer, **pipeline di validazione** |
| `drain.py` | svuota a lotti la coda di Issue `bet` — canale primario |
| `place_bet.py` | applica **una** bet via `workflow_dispatch` — canale semplice |
| `settle.py` | liquidazione parimutuel di un evento |
| `credit.py` | accredito / rettifica (owner) |
| `create_event.py` | apre un evento |
| `register.py` | provisioning utente (id + segreto + config) |
| `rebuild_balances.py` | ricostruzione e verifica dal solo ledger (recovery/dispute) |

`drain.py` e `place_bet.py` **non** hanno regole proprie: chiamano entrambi
`common.validate_and_apply`. Cambiare canale d'ingresso non cambia il denaro.

---

## Invarianti (I1–I10)

Un'implementazione e' conforme se e solo se le preserva tutte.

| # | Invariante | Dove vive |
|---|---|---|
| I1 | ledger integro (seq contigui, `prev_hash`, `hash` ricomputabile) | `common.verify_chain` |
| I2 | saldo = funzione pura del ledger | `common.reduce_balances` |
| I3 | conservazione al settle (assert **prima** di scrivere) | `settle.compute_settlement` |
| I4 | nessun saldo negativo | pipeline, passo 10 |
| I5 | nonce per-utente strettamente crescente | pipeline, passo 9 |
| I6 | `bet_id` applicato una volta sola | `state.applied_bets`, passo 2 |
| I7 | scrittore unico effettivo | `concurrency: ledger-write` nei workflow |
| I8 | pool ricalcolati dal ledger, mai dalla cache | `settle.collect_stakes` |
| I9 | solo il token della Action muta lo stato | `permissions` nei workflow |
| I10 | bet applicata solo se l'HMAC verifica | pipeline, passo 4 |

Verifica in qualunque momento:

```bash
python3 scripts/rebuild_balances.py        # exit 1 se qualcosa non torna
python3 -m unittest discover -s tests -v   # 51 test di conformita'
./scripts/test_smoke.sh                    # end-to-end su arena temporanea
```

---

## Runbook di deployment

1. **Crea il repo** (pubblico o privato, vedi *Privacy* sotto) con questo contenuto.
2. **Segreti Actions**: `USER_SECRETS` (fino a ~1000 utenti) **oppure** `AGE_KEY`
   + `secrets.age` (a scala, registrazione automatizzabile).
3. **Label**: crea la label `bet` nelle Issue (il drain filtra su quella).
4. **Registra gli utenti**: workflow `register` → consegna il `config.json`
   fuori banda.
5. **Accredita**: workflow `credit`.
6. **Apri gli eventi**: workflow `create_event` (o edita `events.json`).
7. **Si gioca**: gli utenti aprono Issue `BET …`; il workflow `drain` le svuota.
8. **Liquidi**: workflow `settle` con `yes`, `no` o `void`.

### Custodia dei segreti

L'HMAC ha bisogno del segreto **in chiaro dentro la Action**: e' il prezzo da
pagare perche' l'autenticita' non dipenda dal client.

* **piccolo** — Actions secret `USER_SECRETS`, un blob
  `{"u_x":"...","u_y":"..."}`. Zero cripto, ma un workflow non puo' scriverlo:
  `register` stampa la coppia e la aggiungi tu.
* **a scala (consigliato)** — `secrets.age` nel repo + `AGE_KEY` come Actions
  secret: `register` decifra, aggiunge, ricifra e committa da solo.
* **locale/test** — `ARENA_SECRETS_FILE=/path/.secrets.json` (mai nel repo:
  e' in `.gitignore`).

### Privacy: tre tier

L'isolamento **in scrittura** e' sempre garantito. Quello in lettura no:

* **Tier 0 — repo pubblico**: chiunque legge tutti i saldi. Semplicissimo
  (letture via `raw`, nessun token). Va bene per un'arena trasparente.
* **Tier 1 — repo privato + token per utente**: il pubblico non vede nulla, ma
  ogni utente registrato vede lo stato di tutti (i fine-grained token non si
  restringono per cartella).
* **Tier 2 — saldi cifrati per utente**: isolamento reciproco vero; i pool per
  evento restano in chiaro (servono per le quote e per il settlement).

Con repo **pubblico** i log dei run sono pubblici: `register` non deve mai
stampare un segreto in chiaro — si usa `--recipient <chiave age>`.

---

## Il client (a-Shell)

```bash
mkdir -p ~/Documents/.arena && mv config.json ~/Documents/.arena/config.json
python3 arena.py events                 # eventi aperti, pool e quote implicite
python3 arena.py balance                # il tuo saldo
python3 arena.py bet ev_derby yes 500   # firma, invia, aspetta la ricevuta
python3 arena.py receipt u_ab12ef34-7   # ricontrolla dopo
```

`config.json` (consegnato dall'owner, §5.3):

```json
{"user_id":"u_ab12ef34","secret":"hex64","repo":"owner/arena",
 "branch":"main","read":"raw","channel":"issue","token":"<solo se serve>"}
```

Il segreto non lascia mai il telefono: viaggia solo l'HMAC. Il saldo locale non
e' mai la verita': comanda `balances.json`.

### Scheletro di firma (se ti scrivi un client tuo)

La stringa canonica ha un **ordine fisso**: non derivarla dal JSON.

```python
import hmac, hashlib, json
from datetime import datetime, timezone

nonce  = last_nonce + 1                      # da balances.json[user_id]
bet_id = f"{user_id}-{nonce}"
bet = {"bet_id": bet_id, "user_id": user_id, "event_id": event_id,
       "side": side, "amount": int(amount), "nonce": nonce,
       "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

msg = "v1|{bet_id}|{user_id}|{event_id}|{side}|{amount}|{nonce}|{ts}".format(**bet)
bet["sig"] = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
```

Invio: apri una Issue con label `bet`, titolo `BET <bet_id>`, corpo con un
blocco ```` ```json ```` che contiene il payload. Poi fai polling su
`receipts.json[bet_id]` finche' non compare `APPLIED` o `REJECTED`.

### Quote implicite

Per un lato con stake `s`, totale `T` e takeout `t = takeout_bps/10000`:

```
moltiplicatore atteso = (1 - t) * T / s        (indefinito se s == 0)
```

Sono **dinamiche**: ogni puntata successiva le cambia. Quella che vedi al
momento dell'invio e' una stima, non una quota bloccata.

---

## Come funziona una scommessa

```
utente          Issue "BET u_x-3"  ──►  workflow drain (gruppo ledger-write)
                                            │
                                            ├─ valida (§8): schema, DUP, utente,
                                            │  HMAC, tipi, evento, finestra,
                                            │  limiti, nonce, saldo
                                            ├─ applica: BET_DEBIT + saldo + pool
                                            ├─ COMMIT (ledger + receipts)  ◄── qui l'effetto e' durevole
                                            └─ poi commenta/chiude la issue
utente          polling receipts.json[bet_id]  ──►  APPLIED / REJECTED + codice
```

**L'ordine e' la difesa contro i crash**: la ricevuta viaggia col commit, la
issue e' solo un effetto collaterale. Consegna at-least-once + apply idempotente
= effetto *once*.

### Codici ricevuta

`OK_APPLIED` · `DUP` (successo) · `ERR_MALFORMED` · `ERR_UNKNOWN_USER` ·
`ERR_SIG` · `ERR_EVENT_MISSING` · `ERR_EVENT_CLOSED` · `ERR_LIMITS` ·
`ERR_NONCE` · `ERR_BALANCE`

### Settlement

```
T             = pool_yes + pool_no                  (dal LEDGER, non dalla cache)
takeout       = T * takeout_bps // 10000
distributable = T - takeout
payout(s)     = s * distributable // pool_vincente   (divisione intera)
dust          = distributable - Σpayout              (va alla casa, non ai vincitori)
```

Rimborso totale (`payout = stake`, takeout 0) se: `outcome = void`, oppure il
lato vincente ha pool 0, oppure il mercato e' monolaterale e
`void_if_one_sided` e' attivo.

---

## Sicurezza in breve

| Minaccia | Difesa | Esito |
|---|---|---|
| modificare il proprio saldo | nessun `contents:write` agli utenti (I9) | bloccato |
| scommettere per un altro | HMAC sul segreto individuale (I10) | `ERR_SIG` |
| replay di una bet | `bet_id` idempotente (I6) + nonce (I5) | `DUP` / `ERR_NONCE` |
| manomettere lo storico | catena di hash (I1) | rilevabile |
| puntare a esito noto | freeze a `close_at` | `ERR_EVENT_CLOSED` |
| doppia spesa concorrente | `concurrency: ledger-write` (I7) + saldo (I4) | bloccato |
| pay-out > pool | assert di conservazione (I3) | run abortito |

**Rischi residui, detti chiaramente.** Il token che apre le Issue e' comune al
client: chi lo estrae puo' fare spam (DoS) o inviare bet per altri `user_id`, ma
quelle falliscono l'HMAC. I soldi restano al sicuro finche' i **segreti
individuali** non trapelano. I segreti arrivano in chiaro alla Action (serve per
l'HMAC): per questo stanno cifrati a riposo e non vanno mai nei log pubblici.

---

## Manutenzione

* **Sharding**: il ledger e' mensile (`YYYY-MM.ndjson`), il reducer legge gli
  shard in ordine di nome.
* **`applied_bets`**: e' un indice; si possono potare i `bet_id` di eventi gia'
  in `settled.json` (non sono piu' ri-applicabili, prenderebbero
  `ERR_EVENT_CLOSED`).
* **`receipts.json`**: GC opzionale delle ricevute di eventi liquidati oltre una
  retention (es. 7 giorni).
* **Backpressure**: se il drain non sta dietro, alza la frequenza del cron; la
  concorrenza garantisce comunque la correttezza.
* **Disallineamenti**: `rebuild_balances.py` dice *cosa* non torna;
  `--write` riallinea la proiezione al ledger (il ledger non si tocca mai).
