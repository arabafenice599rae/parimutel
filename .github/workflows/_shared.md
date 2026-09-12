Nota per chi edita questi workflow:

* ogni workflow che tocca il denaro DEVE stare nel gruppo di concorrenza
  `ledger-write` con `cancel-in-progress: false` (I7): e' quello che rende lo
  scrittore unico ed evita la doppia spesa concorrente;
* `permissions.contents: write` va solo qui, mai agli utenti (I9);
* i segreti utente arrivano da `USER_SECRETS` (deployment piccoli) oppure da
  `secrets.age` + `AGE_KEY` (a scala, §5.2) e non vanno MAI stampati se i log
  del repo sono pubblici (§5.4).
