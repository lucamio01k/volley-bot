# Volley Bot — backlog

Questo file raccoglie le estensioni escluse dalla prima versione EuroVolley.

## P1 — Prossime competizioni

- [ ] **VNL uomini e donne** — creare un provider JSON per l’endpoint ufficiale Volleyball World `/api/v1/volley-tournament/{fromDate}/{toDate}/{tournamentIDs}`. Gli ID sono stagionali e devono restare in `settings.json`.
- [ ] **Mondiali uomini e donne** — verificare se la competizione usa lo stesso provider Volleyball World e aggiungere fixture reali prima dell’attivazione.
- [ ] **Olimpiadi** — analizzare il calendario ufficiale Olympics/FIVB della prossima edizione; non scegliere un aggregatore non ufficiale.
- [ ] **Amichevoli** — introdurre prima un file manuale validato e versionato; valutare API-Sports come discovery e usare FIPAV per la conferma. Piano e ricerca: [SOURCES_AND_PROVIDER_PLAN.md](SOURCES_AND_PROVIDER_PLAN.md).

## P2 — Operatività

- [ ] Dashboard web con stato scheduler, ultimi sync, incidenti, log e pulsanti CLI sicuri.
- [ ] Canale Telegram amministrativo separato opzionale; per la v1 gli errori usano lo stesso canale sportivo.
- [ ] Override manuale di emergenza per aggiungere o correggere una partita se il formato CEV cambia.
- [ ] Esportazione/importazione del database e procedura di backup Raspberry.

## P3 — Contenuti

- [ ] Classifiche dei gironi e percorso nel tabellone.
- [ ] Informazioni ufficiali TV/streaming, solo quando la fonte è stabile e territorialmente corretta.
- [ ] Statistiche essenziali post-partita.

## Criterio comune

Ogni provider nuovo deve avere: fonte ufficiale, identificatore stabile, conversione timezone, cache che non venga cancellata da import falliti, fixture HTML/JSON e `source-check` live.
