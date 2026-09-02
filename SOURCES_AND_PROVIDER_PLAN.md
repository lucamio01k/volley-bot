# Fonti dati e piano provider

Documento di riferimento per estendere Volley Bot oltre EuroVolley, con particolare attenzione alle amichevoli delle nazionali italiane, all'affidabilità delle fonti e al contenimento delle richieste HTTP.

La ricerca e le prove dal vivo riportate qui sono state eseguite il 28 agosto 2026. Gli endpoint non documentati possono cambiare: prima di abilitarli in produzione devono avere fixture di test, monitoraggio e un fallback.

> **Decisione implementativa del 1 settembre 2026:** non verrà mantenuto un file manuale di amichevoli. Il provider API-Sports e i filtri automatici sono implementati ma disattivati finché chiave, ID e casi storici non vengono validati. I passaggi sul file manuale restano sotto come alternativa analizzata, non come percorso scelto.

## Decisione consigliata

Non esiste oggi una singola API pubblica, ufficiale e completa per EuroVolley, competizioni FIVB e amichevoli FIPAV. Conviene adottare una strategia multi-provider con una gerarchia di fiducia:

1. fonti ufficiali strutturate per il calendario ordinario;
2. FIPAV o inserimento manuale per confermare le amichevoli dell'Italia;
3. un aggregatore come API-Sports soltanto per scoprire possibili amichevoli mancanti;
4. nessuna notifica pubblica per una partita amichevole ancora non confermata.

La combinazione iniziale raccomandata è:

- **EuroVolley:** mantenere il provider HTML CEV esistente;
- **VNL e Mondiali FIVB:** Volleyball World JSON come integrazione semplice, con FIVB VIS come fonte ufficiale e controllo;
- **amichevoli:** API-Sports con filtri esatti su lega, squadra Italia, genere e stagione;
- **attivazione:** prova controllata con il piano gratuito e provider disabilitato fino al superamento dei casi storici.

## Confronto delle fonti

| Fonte | Ufficiale | Formato/accesso | Copertura utile | Amichevoli | Uso consigliato |
|---|---:|---|---|---|---|
| [FIVB VIS Web Service](https://www.fivb.org/VisSDK/VisWebService/) | sì | XML, accesso guest ai dati pubblici | tornei FIVB, squadre, calendario, risultati, ranking | non risultano le amichevoli italiane verificate | fonte ufficiale generale e aggiornamenti incrementali |
| [Volleyball World](https://en.volleyballworld.com/global-schedule/) | sì | endpoint JSON interni, senza chiave nelle prove | VNL, Mondiali e altri eventi Volleyball World | assenti nei casi italiani verificati | provider pratico per competizioni FIVB, con fallback |
| [CEV Competition Area](https://www-old.cev.eu/Competition-Area/) | sì | HTML, nessuna API pubblica documentata | EuroVolley e competizioni CEV | non è una fonte generale per le amichevoli | conservare il parser esistente |
| [FIPAV](https://www.federvolley.it/nazionali) | sì | articoli web, CMS non pubblico | programmi e comunicati delle nazionali italiane | sì, è la conferma più autorevole | verifica o inserimento manuale |
| [API-Sports Volleyball](https://api-sports.io/sports/volleyball) | no | REST JSON con API key | calendario e storico di molte competizioni | categorie dichiarate esplicitamente | discovery, dopo un test di copertura |
| [Highlightly](https://highlightly.net/volleyball-api/) | no | REST JSON con API key | oltre 230 leghe dichiarate | copertura non dichiarata chiaramente | alternativa secondaria da testare |
| [Sportradar Volleyball](https://developer.sportradar.com/volleyball/reference/indoor-volleyball-overview) | no | commerciale e autenticato | feed professionale | nessuna copertura Friendly emersa dalla matrice pubblica | eccessivo per le esigenze attuali |
| SofaScore | no | API interna non supportata | ampia; ha mostrato amichevoli italiane | sì nei test visivi | non usare: accesso server bloccato con HTTP 403 |

## Note sulle fonti principali

### FIVB VIS Web Service

È la fonte ufficiale strutturata più interessante. L'endpoint è:

```text
https://www.fivb.org/Vis2009/XmlRequest.asmx
```

L'accesso guest ha risposto senza autenticazione per i dati pubblici. Il servizio permette di ottenere catalogo tornei, squadre, incontri, orari UTC/locali, città, impianti, risultati, pool e ranking. Il parametro `Version` permette aggiornamenti incrementali e riduce il trasferimento di dati invariati.

Esempio di catalogo tornei:

```xml
<Request Type="GetVolleyTournamentList"
         Fields="No Code Name StartDate EndDate">
  <Filter Seasons="2026" Statuses="Scheduled Running Finished"/>
</Request>
```

Esempio di calendario di un torneo:

```xml
<Request Type="GetVolleyMatchList"
         Fields="No NoInTournament DateTimeLocal DateTimeUtc City Hall MatchPointsA MatchPointsB Status">
  <Filter NoTournament="1662"/>
  <Relation Name="TeamA" Fields="Code Name"/>
  <Relation Name="TeamB" Fields="Code Name"/>
</Request>
```

Nella prova 2026 sono stati trovati 89 tornei, 116 incontri nella VNL femminile e gli ID VNL `1661` (uomini) e `1662` (donne). La copertura non va però confusa con completezza universale: le amichevoli italiane cercate non erano presenti e alcuni tornei CEV potevano comparire in ritardo o solo parzialmente. Per un uso formale in produzione, la documentazione suggerisce di richiedere un identificativo applicazione a `vis.sdk@fivb.org`.

### Volleyball World JSON

Il calendario globale usa endpoint JSON interni come:

```text
/api/v1/globalschedule/{fromDate}/{toDate}
/api/v1/volley-tournament/{fromDate}/{toDate}/{tournamentIDs}
/api/v1/globalschedule/matchdays/{year}/{utcOffset}
```

Le prove hanno restituito HTTP 200, CORS aperto e campi sufficienti per il bot: ID partita stabile, UTC, torneo, fase, squadre, città, stato e punteggi dei set. Una singola finestra dal 20 agosto al 10 settembre 2026 ha restituito 387 partite di 13 tornei.

È più semplice da integrare rispetto a VIS, ma è un'API interna non documentata e può cambiare. Non conteneva EuroVolley né l'amichevole Italia-Germania del 26 agosto 2026. Va quindi usata per VNL/Mondiali, protetta da fixture JSON e fallback VIS, non come fonte universale.

### CEV

Non è emersa un'API pubblica documentata per EuroVolley. Il progetto usa correttamente le pagine ufficiali della Competition Area e gli ID `1572` (uomini) e `1573` (donne). Il parser attuale conserva gli slot TBD e non elimina la cache dopo risposte vuote o non riconosciute: queste protezioni devono restare.

Non conviene sostituire questo provider finché un'altra fonte non dimostra, con un confronto completo, di coprire lo stesso tabellone e i cambi di accoppiamento.

### FIPAV e amichevoli

Le pagine ufficiali di riferimento sono:

- [programma amichevoli Italia femminile](https://www.federvolley.it/il-programma-delle-amichevoli-italia);
- [programma amichevoli Italia maschile](https://www.federvolley.it/il-programma-delle-amichevoli-italia-0);
- [nazionali FIPAV](https://www.federvolley.it/nazionali).

Il nuovo sito è alimentato da un CMS Drupal protetto e non espone un feed pubblico stabile per gli incontri. Date, sedi e avversarie possono apparire in articoli iniziali e poi essere corrette da comunicati successivi. FIPAV è quindi un'ottima fonte autorevole, ma al momento non un provider automatico affidabile.

### API-Sports Volleyball

È il candidato più promettente per scoprire le amichevoli perché la [pagina di copertura](https://api-sports.io/sports/volleyball) e la [documentazione Volleyball v1](https://api-sports.io/documentation/volleyball/v1) dichiarano esplicitamente:

- `Club Friendly`;
- `Club Friendly Women`;
- `Friendly International`;
- `Friendly International Women`.

Il servizio dichiara 263 leghe/coppe, 19 anni e oltre 212.000 partite; il piano gratuito indicato offre 100 richieste al giorno. Serve una chiave API e non è stato possibile verificare senza chiave che le specifiche partite dell'Italia siano effettivamente presenti.

Prima dell'integrazione va eseguito un piccolo test di accettazione su incontri noti:

- Italia-Francia femminile, 14 e 15 maggio 2026;
- Italia-Argentina maschile, 5 luglio 2026;
- Italia-Germania maschile, 26 agosto 2026.

Il test deve verificare non solo la presenza, ma anche ID stabili, orario e timezone, sede, stato finale, punteggio dei set e rapidità degli aggiornamenti. Se il test fallisce, non vale la pena aggiungere complessità al bot.

### Fonti esaminate ma non prioritarie

- [Olympic Data Feed](https://odf.olympictech.org/) pubblica specifiche tecniche, non un feed live liberamente riutilizzabile: sarà utile per capire il modello dati delle Olimpiadi, non come sorgente attuale.
- Goalserve, Broadage e Data Sports Group sono offerte commerciali o orientate anche a quote e scommesse; non mostrano al momento un vantaggio sufficiente per questo bot.
- Highlightly merita un test solo se API-Sports non supera la verifica sulle amichevoli italiane.
- SofaScore può essere usato come controllo umano occasionale, ma non come dipendenza software: gli endpoint interni non sono supportati e nelle prove hanno risposto `403 Forbidden` alle richieste server.

## Modello di fiducia e riconciliazione

Ogni incontro dovrebbe avere uno stato editoriale separato dallo stato sportivo:

```text
discovered -> confirmed -> published
                  |
               rejected
```

- `discovered`: trovato da un aggregatore, non genera notifiche;
- `confirmed`: confrontato con FIPAV o inserito manualmente con fonte ufficiale;
- `published`: ammesso nei digest, promemoria e risultati;
- `rejected`: falso positivo, duplicato o informazione superata.

Priorità proposta, dalla più alta alla più bassa:

1. override manuale esplicito;
2. fonte ufficiale della competizione o FIPAV;
3. provider ufficiale strutturato FIVB/Volleyball World;
4. aggregatore commerciale.

Un aggregatore può scoprire una partita, ma non deve sovrascrivere automaticamente data, risultato o annullamento già confermati da una fonte ufficiale. I record devono conservare `provider`, `provider_match_id`, `source_url`, `source_priority`, `discovery_status`, `confirmed_at` e possibilmente l'hash dell'ultimo payload.

Per evitare duplicati tra provider, non basta inserire il provider nella chiave primaria. Serve una chiave canonica o una tabella di mapping tra incontro interno e ID esterni. Una prima chiave di confronto può usare nazionale, avversaria, genere e data locale, lasciando sempre la possibilità di revisione manuale.

## Budget richieste attuale

Lo scheduler si sveglia ogni 60 minuti, ma `_sync_due()` limita le richieste reali per ciascuna delle due competizioni CEV:

| Scenario | Cadenza per competizione | Totale CEV stimato |
|---|---:|---:|
| entrambe attive | 24 richieste/giorno | 48 richieste/giorno |
| entrambe inattive | 4 richieste/giorno | 8 richieste/giorno |

Il costo più alto è il controllo risultati. Oggi ogni partita non finale viene interrogata singolarmente ogni 10 minuti, da 45 minuti dopo l'inizio fino a 5 ore: in teoria circa 26 richieste per partita. Il controllo Telegram ogni 15 minuti aggiunge 96 chiamate al giorno, ma non consuma la quota delle fonti sportive. Digest e promemoria lavorano sul database e non fanno richieste alle fonti.

## Strategia per ridurre le richieste

### 1. Preferire sempre richieste aggregate

- Con VIS, una `GetVolleyMatchList` per torneo aggiorna calendario e risultati di molte partite.
- Con Volleyball World, una finestra di date aggiorna più tornei e incontri in una sola chiamata.
- Con API-Sports, interrogare solo le due categorie internazionali maschile/femminile, una finestra temporale limitata e, se disponibile, l'ID dell'Italia.
- Evitare una richiesta di dettaglio per ogni partita quando il feed di lista contiene già stato e risultato.

### 2. Polling adattivo del calendario

Una politica iniziale prudente:

| Distanza dalla prossima partita o fase | Cadenza suggerita |
|---|---:|
| oltre 7 giorni | una volta al giorno |
| tra 1 e 7 giorni | ogni 6 ore |
| giorno partita, prima dell'inizio | ogni 1–2 ore |
| torneo ufficiale attivo con tabellone dinamico | ogni ora |
| ricerca amichevoli API-Sports | 1–2 volte al giorno |

Con due chiamate API-Sports al giorno per genere si resta a circa 4 richieste/giorno, molto sotto la quota gratuita di 100. Gli ID di lega, squadra e torneo devono essere scoperti una volta e messi in cache, non ricercati a ogni sincronizzazione.

### 3. Polling adattivo dei risultati

Per la pallavolo è poco utile iniziare dopo soli 45 minuti. La prima verifica può avvenire circa 90 minuti dopo l'orario d'inizio, quindi usare backoff a 15, 30 e 60 minuti. Il polling termina appena il risultato è finale. Se il provider espone un endpoint di torneo, una sola richiesta aggiorna tutte le partite in corso.

Questa è l'ottimizzazione prioritaria del codice esistente: riduce molto più traffico di qualsiasi modifica marginale alla sincronizzazione CEV.

### 4. Cache e aggiornamenti condizionali

- salvare `ETag` e usare `If-None-Match` dove il server lo supporta;
- usare `Version` per gli aggiornamenti incrementali VIS;
- memorizzare l'hash normalizzato del payload e non riscrivere il database se è invariato;
- applicare jitter agli orari per evitare che tutte le fonti vengano chiamate nello stesso minuto;
- mantenere l'ultimo dato valido quando la risposta è vuota, malformata o incompleta.

## Architettura proposta

`tasks.py` istanzia oggi `CEVClient` direttamente sia per il calendario sia per i risultati. Prima di aggiungere fonti conviene introdurre interfacce semplici:

```python
class ScheduleProvider:
    def fetch_competition(self, competition, state): ...

class ResultProvider:
    def fetch_results(self, competition, matches, state): ...
```

`fetch_results` dovrebbe essere aggregato quando possibile. Ogni provider mantiene uno stato indipendente (`last_sync`, `etag`, `version`, `payload_hash`, ultimo errore) e una propria cadenza. Le chiavi di stato non devono più essere hardcoded come `cev:*`.

Anche la configurazione delle competizioni deve specificare almeno `provider`, `provider_competition_id` e strategia di refresh. L'attuale `competition_id` intero è adatto alla CEV, ma provider futuri possono usare ID stringa o più parametri.

Prima di pubblicare competizioni diverse da EuroVolley va inoltre corretto `format_result()`, che oggi scrive sempre “EuroVolley”: deve usare `competition_name` e, per le amichevoli, una categoria coerente.

## Alternativa non scelta: file manuale per le amichevoli

Questa soluzione era stata proposta come primo passo affidabile, ma è stata scartata a favore dell’automazione. Se in futuro servisse un fallback editoriale, il percorso corretto sarebbe `fixtures/manual_matches.json`, perché la directory `data/` è esclusa da Git.

Schema minimo suggerito:

```json
{
  "matches": [
    {
      "id": "friendly-2026-08-26-ita-ger-men",
      "competition_name": "Amichevole internazionale",
      "gender": "men",
      "home_team": "ITALY",
      "away_team": "GERMANY",
      "scheduled_at": "2026-08-26T21:00:00+02:00",
      "venue": null,
      "source_url": "https://www.federvolley.it/...",
      "confirmed_at": "2026-08-20T12:00:00+02:00",
      "status": "scheduled"
    }
  ]
}
```

Il caricatore deve validare campi, timezone, genere, squadre, duplicati e URL della fonte. Un errore nel file non deve cancellare gli incontri già validi nel database. L'ID manuale resta stabile anche se cambiano orario o sede.

## Piano di lavoro consigliato

### Fase 0 — Misurare senza cambiare comportamento

- aggiungere contatori per richieste, risposte `304`, errori e record modificati per provider;
- misurare per una settimana il polling risultati reale;
- definire un tetto giornaliero configurabile per ogni provider commerciale.

### Fase 1 — Amichevoli automatiche controllate

- configurare la chiave gratuita e identificare leghe/squadre con `api-sports-probe`;
- validare i casi storici con il provider ancora disabilitato;
- rendere formatter e notifiche neutrali rispetto a EuroVolley;
- aggiungere test su timezone, deduplicazione, modifica orario e cancellazione/annullamento.

Questa fase mantiene il consumo ordinario intorno a quattro richieste al giorno.

### Fase 2 — Astrarre i provider e ottimizzare i risultati

- separare provider calendario e risultati da `tasks.py`;
- introdurre richieste batch e backoff adattivo;
- salvare stato di sincronizzazione, ETag/versione e metriche per provider;
- conservare tutte le protezioni del parser CEV.

### Fase 3 — VNL e Mondiali

- integrare Volleyball World JSON dietro fixture e `source-check`;
- confrontare un campione con VIS;
- predisporre VIS come fallback o fonte primaria se l'endpoint JSON interno cambia.

### Fase 4 — Discovery automatica amichevoli

- ottenere una chiave API-Sports gratuita;
- eseguire il test sui tre casi noti senza pubblicare dati;
- se la copertura è sufficiente, salvare i risultati come `discovered`;
- prevedere conferma manuale/FIPAV prima di `published`;
- interrompere la prova se aggiunge falsi positivi o non copre gli incontri italiani.

## Criteri di accettazione per ogni nuovo provider

- fonte e termini d'uso documentati;
- identificativi stabili per competizione e partita;
- orari espliciti con timezone e conversione UTC testata;
- fixture HTML/XML/JSON nei test, senza dipendere solo dalla rete;
- `source-check` live separato dai test deterministici;
- nessuna cancellazione della cache dopo import vuoto o malformato;
- deduplicazione tra provider;
- limite richieste, timeout, backoff e jitter configurabili;
- alert dopo errori persistenti e messaggio di ripristino;
- fallback o procedura manuale documentata.

## Scelta finale

Per le amichevoli è stato scelto API-Sports con piano gratuito, filtri automatici e attivazione subordinata alla validazione. Per le altre partite, Volleyball World rende rapida l'integrazione di VNL e Mondiali, mentre VIS offre la base ufficiale più solida per il lungo periodo.

Prima di aggiungere qualunque nuova fonte, conviene però ottimizzare il polling risultati esistente e introdurre l'astrazione dei provider: così l'estensione ridurrà, invece di moltiplicare, il numero complessivo di richieste.
