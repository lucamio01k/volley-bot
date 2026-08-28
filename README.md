# Italia Volley Telegram Bot

Bot Telegram per le partite delle nazionali italiane senior maschile e femminile. La prima versione segue CEV EuroVolley 2026, compresi gli accoppiamenti dinamici della fase a eliminazione diretta.

## Funzioni

- importa il tabellone ufficiale CEV uomini (`1572`) e donne (`1573`);
- conserva anche gli slot con squadre ancora da definire;
- riconosce quando l’Italia viene assegnata a un nuovo turno;
- invia riepilogo settimanale, messaggio del giorno gara, promemoria, variazioni e risultati;
- usa SQLite per evitare duplicati dopo i riavvii;
- segnala nello stesso canale gli errori persistenti e il successivo ripristino;
- non cancella il cache quando la fonte è vuota o non riconosciuta.

La CEV non offre un’API pubblica documentata per EuroVolley: il provider esegue richieste HTTP alle pagine ufficiali e analizza gli identificatori strutturati dell’HTML. FIPAV e i PDF ufficiali sono fonti di controllo manuale.

## Installazione

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Impostare in `.env`:

```text
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHANNEL_ID=...
```

Orari, intervalli e singoli tipi di notifica sono modificabili in `settings.json` tramite il rispettivo campo `enabled`.

## Verifica iniziale

```bash
.venv/bin/python cli.py source-check
.venv/bin/python cli.py sync --force
.venv/bin/python cli.py matches
.venv/bin/python cli.py week
.venv/bin/python cli.py today
.venv/bin/python cli.py test
```

I comandi di riepilogo producono solo un’anteprima. Aggiungere `--send` per inviare e `--force` per ignorare la deduplicazione quando supportato.

## CLI

```text
sync [--send] [--force]  aggiorna il cache CEV
matches                  mostra le partite dell’Italia
bracket                  mostra tutto il tabellone, inclusi i TBD
week [--date ...]        anteprima/invio della settimana
today [--date ...]       anteprima/invio del giorno
reminders                promemoria attualmente dovuti
results                  risultati da controllare
source-check             valida dal vivo entrambe le fonti CEV
alerts-status            mostra contatori e incidenti
test [--send]            verifica Telegram
db-status                mostra i contatori SQLite
```

## Avvio e riavvio

Per lo sviluppo:

```bash
./restart.sh
```

Lo script usa `data/volley-bot.pid`, verifica che il PID appartenga a questo progetto e scrive l’output in `data/volley-bot.out`.

Per Raspberry, copiare `volley-bot.service` in `/etc/systemd/system/`, correggere utente e percorso se necessario, quindi:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now volley-bot.service
./restart-service.sh
```

## Scheduler predefinito

- sync CEV ogni 60 minuti durante il torneo, con skip automatico fino a 6 ore fuori torneo;
- riepilogo lunedì alle 09:00;
- messaggio del giorno gara alle 08:30;
- promemoria due ore prima;
- controllo risultati ogni 10 minuti da 45 minuti dopo l’inizio fino a cinque ore;
- controllo Telegram ogni 15 minuti.

Tutti gli orari utente sono in `Europe/Rome`; le ore locali CEV vengono convertite in UTC prima del salvataggio.

## Avvisi operativi

Dopo tre errori consecutivi dello stesso componente viene inviato un singolo messaggio `⚠️ Errore bot volley`. Il bot continua a riprovare senza ripetere lo stesso avviso. Alla prima sincronizzazione riuscita invia `✅ Bot volley ripristinato`.

Gli avvisi non contengono token, traceback o HTML grezzo. Lo stato è visibile anche con:

```bash
.venv/bin/python cli.py alerts-status
```

## Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Le estensioni future sono tracciate in [PENDING.md](PENDING.md).
