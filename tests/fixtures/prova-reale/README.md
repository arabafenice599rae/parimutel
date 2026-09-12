# Fixture: la prima prova reale su GitHub Actions

Stato del ledger prodotto dal **primo giro end-to-end vero** su
`arabafenice599rae/parimutel`, il 2026-09-12. Non e' stato scritto a mano:
ogni byte qui dentro l'hanno prodotto i workflow su runner GitHub.

## Cosa e' successo

| Passo | Come |
|---|---|
| Evento aperto, due utenti accreditati | seed via PR ([#4](https://github.com/arabafenice599rae/parimutel/pull/4)) |
| 4 issue `BET` aperte insieme | trigger `issues` → drain automatico |
| `u_03b325ba` punta 3000 su `yes` | `OK_APPLIED` |
| `u_1b676e26` punta 2000 su `no` | `OK_APPLIED` |
| bet firmata col segreto sbagliato | `ERR_SIG` — mai entrata nel ledger |
| copia identica di una bet valida | `DUP` — nessuna doppia spesa |
| una bet su evento inesistente (sonda) | `ERR_EVENT_MISSING` |
| liquidazione con esito `no` | `settle` workflow |
| **seconda** liquidazione dello stesso evento | no-op idempotente, zero entry nuove |

Durante il drain, quattro trigger simultanei sono collassati: un run ha drenato
tutta la coda, gli altri sono stati cancellati da `concurrency` e l'ultimo ha
trovato la coda vuota. Nessuna bet persa.

## A cosa serve adesso

`tests/test_arena.py::TestFixtureProvaReale` la rigioca a ogni CI e verifica
che il reducer, la catena e la matematica del settlement producano **ancora**
esattamente questi numeri. E' l'unico test della suite i cui dati non me li
sono inventati io: se una modifica futura cambia il significato di una entry,
qui si vede subito.

## Nota

Non e' contabilita' di produzione: il ledger reale e' ripartito da zero dopo
questa prova. Gli utenti sono usa-e-getta e i loro segreti non sono mai
esistiti fuori da un Actions secret poi sostituito.
