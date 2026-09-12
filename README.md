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
| `scripts/stress_test.py` | stress test di concorrenza (scrittori simultanei, crash) |

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
| `stress_test.py` | molti scrittori concorrenti contro lo stesso repo |
| `verify_state.py` | verifica profonda di uno stato: la definizione di "arena sana" |
| `acceptance_test.py` | test di accettazione su GitHub vero (issue, drain, payout) |

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
python3 scripts/verify_state.py            # exit 1 se qualcosa non torna
python3 scripts/rebuild_balances.py        # solo catena + saldi (recovery)
python3 -m unittest discover -s tests -v   # 52 test di conformita'
./scripts/test_smoke.sh                    # end-to-end su arena temporanea
python3 scripts/stress_test.py             # 5 scrittori simultanei + crash
```

---

## Concorrenza: chi garantisce cosa

**`git commit` non e' il confine atomico globale — lo e' `git push`.** Finche'
il push non passa, il lavoro di un run non esiste per nessun altro. Da qui
tutto il resto:

```
RUN A                      RUN B
checkout HEAD X            checkout HEAD X
calcola il lotto           calcola il lotto
commit A                   commit B
push A  ✓                  push B  ✗ non-fast-forward
                           └─► fetch + reset --hard origin/main
                               RICALCOLA il lotto sullo stato nuovo
                               (le bet di A diventano DUP)
                               push  ✓
```

Il commit di B non viene forzato e non viene riproposto: descrive uno stato che
non esiste piu'. `drain.py` rilegge lo stato da zero e **rifa'** il lotto
(`--max-attempts`, default 3); esaurite le prove esce con 11, le issue restano
aperte e il cron riprende da li'.

Tre livelli di difesa, in ordine di forza:

1. **`concurrency: ledger-write`** — su GitHub i run sono in fila, la corsa non
   avviene quasi mai.
2. **Push non forzato** — se avviene, il perdente se ne accorge.
3. **Idempotenza (I6) + nonce (I5)** — il perdente puo' rifare tutto senza
   applicare niente due volte.

Lo stress test toglie il primo livello di proposito e verifica che gli altri due
bastino.

### Codici di uscita

Un guasto non deve mai travestirsi da rifiuto: in un sistema contabile e' la
differenza fra "l'utente ha sbagliato" e "abbiamo un bug".

| Codice | Significato | Il run deve... |
|---:|---|---|
| 0 | lavoro svolto (applicata, DUP, coda vuota) | passare |
| 10 | bet **rifiutata** dalla validazione, ricevuta scritta | passare |
| 11 | push rifiutato da uno scrittore concorrente | fallire e ripartire |
| 1 | errore di infrastruttura (git, filesystem, bug) | fallire |
| 2 | invocazione o configurazione sbagliata | fallire |

Per questo nei workflow non c'e' nessun `|| true`: solo il 10 viene tradotto in
successo, esplicitamente.

---

## Stress test di concorrenza

```bash
python3 scripts/stress_test.py                                  # profilo veloce
python3 scripts/stress_test.py --users 100 --bets 8 --runners 12 --crash-rate 0.3
python3 scripts/stress_test.py --keep                           # conserva l'arena
```

Costruisce un bare repo, accredita N utenti, riempie una coda di issue e lancia
R processi `drain` **davvero simultanei, senza la serializzazione di GitHub** —
il caso peggiore. Dentro la coda ci sono consegne doppie, firme forgiate, utenti
inesistenti, sforamenti di saldo e di limite, un evento chiuso, corpi
illeggibili e il replay di un nonce vecchio. Durante la corsa i runner vengono
uccisi con SIGKILL in punti casuali, anche fra il push e la chiusura delle
issue. Alla fine piu' processi liquidano lo stesso evento insieme.

Poi ricontrolla tutto da un clone pulito:

* I1 catena, I2 saldi ricostruiti, I6 indice, I4 saldi mai negativi
  (nemmeno **transitoriamente**, rigiocando il ledger entry per entry);
* una sola `BET_DEBIT` per `bet_id`, nonce per utente strettamente crescente;
* ogni bet valida applicata **esattamente una volta**, ogni bet ostile fermata
  **dal controllo giusto** (verificare solo "rifiutata" nasconderebbe un
  controllo che ne maschera un altro);
* le ricevute non mentono: cio' che e' nel ledger risulta `APPLIED`, cio' che
  non c'e' risulta `REJECTED`;
* pool in cache == pool ricalcolati, un solo settlement per evento;
* **conservazione globale**: `Σ accrediti == Σ (available + at_risk) + Σ incasso
  della casa`;
* la coda si e' svuotata: le issue sono tutte chiuse.

Misura tipica (100 utenti, 800 bet, 12 runner, 30% di crash): ~176 run di drain,
~52 crash, ~77 conflitti di push, zero violazioni.

### Test di accettazione su GitHub vero

Lo stress test prova la **logica** contro un bare repo locale. L'accettazione
prova l'**integrazione**: issue vere, drain veri, runner veri.

```bash
export GITHUB_TOKEN=<pat con issues:write>
export ARENA_SECRETS_FILE=~/.arena/test-secrets.json

python3 scripts/acceptance_test.py --repo owner/arena --plan    # cosa cliccare
python3 scripts/acceptance_test.py --repo owner/arena --bets    # apre le issue
python3 scripts/acceptance_test.py --repo owner/arena --verify  # verifica
```

Le fasi sono separate perche' due passaggi richiedono un umano: `create_event`,
`credit` e `settle` passano da `workflow_dispatch`, che un token
d'integrazione non puo' innescare. Lo script dice esattamente cosa cliccare.

I payout vengono **ricalcolati in modo indipendente** dalla formula della spec,
non chiamando `settle.compute_settlement`: verificare il codice con se stesso
non proverebbe niente.

### La prima prova reale e' diventata un test

`tests/fixtures/prova-reale/` contiene il ledger prodotto dal primo giro
end-to-end vero su GitHub Actions: due bet applicate, una firma forgiata
respinta, una consegna doppia, un settlement e un secondo settlement no-op.
`TestFixtureProvaReale` lo rigioca a ogni CI. E' l'unico test della suite i cui
dati non sono inventati.

### Il test ha i denti

Un test verde che non puo' fallire non dimostra niente. Disattivando a turno una
difesa, lo stress test la becca:

| Mutazione | Cosa succede |
|---|---|
| `git push --force` | il settlement di un run viene sovrascritto: record perso |
| idempotenza I6 spenta | consegne doppie riscrivono le ricevute: bet applicate riportate `REJECTED` |
| controllo saldo I4 spento | `available` va sotto zero, anche transitoriamente |
| nonce I5 spento | il replay del nonce vecchio entra nel ledger |

Nota onesta: I5 e I6 si coprono quasi del tutto a vicenda, perche' `bet_id` e'
`<user_id>-<nonce>`. L'unico caso in cui serve davvero I5 e' il replay di una
bet **rifiutata in precedenza** (quindi assente dall'indice di idempotenza) con
un nonce ormai superato — ed e' esattamente il caso che la coda ostile include.

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

### Installazione sul telefono

In a-Shell, tre comandi:

```bash
cd ~/Documents
curl -O https://raw.githubusercontent.com/<owner>/<arena>/main/client/arena.py
python3 arena.py init
```

`init` chiede un valore per volta (user_id, secret, repo, token), scrive
`~/Documents/.arena/config.json` con permessi `600` e **verifica subito che
l'arena risponda**. Scrivere a mano un JSON con dentro 64 caratteri
esadecimali, su una tastiera da telefono, e' il modo migliore per sbagliare un
carattere e non capire perche' ogni bet prende `ERR_SIG`.

Per aggiornare il client basta rilanciare il `curl`: e' un file solo, senza
dipendenze fuori dalla stdlib.

### Token

Serve **solo per scommettere** (aprire una issue); leggere eventi e saldi
funziona senza. Su GitHub: *Settings → Developer settings → Personal access
tokens → Fine-grained tokens*, con **Only select repositories** → l'arena, e
come permesso **Issues: Read and write**. Niente altro: quel token non deve
poter scrivere codice.

### Uso quotidiano

```bash
python3 arena.py events                 # eventi aperti, pool e quote implicite
python3 arena.py balance                # il tuo saldo
python3 arena.py bet ev_derby yes 500   # firma, invia, aspetta la ricevuta
python3 arena.py receipt u_ab12ef34-7   # ricontrolla dopo
```

`config.json` (§5.3) — se preferisci scriverlo a mano:

```json
{"user_id":"u_ab12ef34","secret":"hex64","repo":"owner/arena",
 "branch":"main","read":"raw","channel":"issue","token":"<solo se serve>"}
```

Il segreto non lascia mai il telefono: viaggia solo l'HMAC. Il saldo locale non
e' mai la verita': comanda `balances.json`.

**Il nonce viene "bruciato" prima dell'invio, di proposito.** Il client salva
`nonce.json` *prima* di chiamare GitHub: se la rete cade subito dopo, quel nonce
resta consumato anche se la bet non e' mai arrivata, e la prossima partira' da
N+1. E' un fastidio, non una perdita: nessun soldo si muove. L'alternativa —
riusare il nonce dopo un errore di rete — e' peggiore, perche' una richiesta
"fallita" puo' essere arrivata lo stesso e si finirebbe per firmare due bet
diverse con lo stesso nonce. Meglio un buco nella numerazione che un replay
ambiguo.

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
