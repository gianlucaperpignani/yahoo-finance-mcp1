# Archivio persistente — fase1, versione1.0.0

Stato: codice predisposto; NON attivo finché configurazione e collaudo live non
sono riusciti. Nessun ordine, nuova soglia o worker di raccolta viene attivato.
La cache Render resta volatile. Questo ponte conserva oggetti JSON immutabili
su Drive e una ricevuta nel Registro esistente; il client verifica una lettura
separata prima di permettere di liberarli dalla memoria.

## Configurazione iniziale del proprietario Google

1. Aprire un progetto separato su https://script.google.com/ e incollare
   `ArchiveGateway.gs`. Non riusare script di altri progetti.
2. In Impostazioni progetto → Proprietà script impostare:
   - `REGISTRY_ID`: ID del Registro Trading eToro originale del progetto.
   - `ARCHIVE_FOLDER_ID`: cartella privata Drive destinata agli archivi tecnici.
   - `ARCHIVE_HMAC_SECRET`: un segreto casuale di almeno32 caratteri, generato
     localmente con `python -c "import secrets; print(secrets.token_hex(32))"`.
     Non inviarlo in chat, GitHub o file del progetto.
3. Distribuisci → Nuova distribuzione → Applicazione web. Esegui come il
   proprietario del Registro; consenti richieste esterne all'applicazione web.
   Il gateway verifica HMAC per OGNI richiesta: l'URL da solo non dà accesso.
   Autorizzare Google soltanto nel proprio account corretto. Se l'organizzazione
   vieta questa modalità, fermarsi: serve un diverso accesso OAuth, non un bypass.
4. In Render → Environment impostare `ARCHIVE_WEBAPP_URL` all'URL produzione
   `https://script.google.com/macros/s/.../exec` e `ARCHIVE_HMAC_SECRET` allo
   stesso segreto. Queste variabili sono separate dalle chiavi eToro.

## Collaudo prima dell'attivazione

Dal codice aggiornato e con le variabili configurate:

```bash
python archive_client.py status
python archive_client.py self-test
```

`status` controlla accesso e schema, non dichiara durabilità. `self-test` scrive
un piccolo oggetto tecnico privo di dati di conto, lo rilegge e lo recupera con
un nuovo client. Richiedere `durable_verified=true` e
`restart_recovery_verified=true`; conservare ricevuta, hash e riga del Registro.
Un timeout è un esito incerto: riprovare lo stesso put conserva object_id e non
deve duplicare file/indice. Una scrittura senza readback NON autorizza la pulizia
della cache. La prova con client nuovo non equivale da sola a riavvio reale di
Render: anche quel collaudo va svolto prima di dichiarare chiusa la fase1.

## Contratto e limiti

- Nessuna dipendenza Python nuova; HMAC e HTTPS, nessun segreto nei log.
- Limite tecnico2MB per oggetto; suddividere input più grandi in oggetti
  referenziati da un manifest, senza duplicare i sorgenti a ogni ciclo.
- Oggetti identificati da tipo+ciclo+hash, immutabili; nessuna cancellazione.
- Il gateway scrive solo un indice tecnico in Registro; non cambia Snapshot,
  Curva, formule, posizioni o Esecuzioni. Non crea un Registro parallelo.
- LockService serializza le richieste di QUESTO gateway; non blocca scrittori
  esterni che usano direttamente Sheets. Le append concorrenti da ChatGPT
  devono essere coordinate prima di attivare un worker automatico. Nessuna
  garanzia di compare-and-swap globale è dichiarata.
- Il worker di raccolta, i limiti cache e l'integrazione nel cron sono fasi
  successive. La presenza di questo codice non prova la raccolta riuscita.

Test locali senza Google o dati reali:

```bash
python -m unittest discover -s tests -p test_archive_client.py -v
node tests/test_archive_gateway.cjs
```

## Integrazione server 1.7.51

Al primo accesso HTTP, un thread nel processo servente verifica automaticamente
status, put e readback firmati. `/status` espone `persistent_archive` senza
segreti, URL, contenuti privati o ID Drive. Le richieste HTTP non aspettano il
collaudo. Una sola esecuzione per PID; nessun retry automatico dopo un errore.
Un unico oggetto TECHNICAL_TEST immutabile viene riusato tra avvii, senza
aggiungere una riga a ogni richiesta. Nessuna cache viene eliminata.

Attendere `state=PASS` e `durable_storage_verified=true`. Annotare
`started_at_utc`, riavviare realmente Render e richiamare `/status`: la data
di avvio deve cambiare e `previous_object_recovered=true`. Questo confronto
esterno costituisce la prova controllata di riavvio; il server non la dichiara
automaticamente (`controlled_restart_verified=false`). Un altro worker può
infatti recuperare l'oggetto senza che il servizio sia stato riavviato.
La raccolta finanziaria automatica resta una fase successiva.

## Collettore scanner 1.7.52

I job `scan_cycle_candidates` archiviano automaticamente gli artefatti numerici
completati, prima di rispondere COMPLETE. La shortlist globale resta al massimo
10 titoli; tutti gli altri dati rimangono nel Python/Drive. `scan_complete`
continua a descrivere la copertura, non il salvataggio o la validità di ticket.
Controllare separatamente `opportunity_archive.persistence.durable_verified`.

Il riferimento `opportunity_archive.path` include un object_id quando la
persistenza è verificata; conservare quel riferimento nel checkpoint del
collettore. Permette di recuperare lo stesso artefatto dopo perdita della RAM
o riavvio. Il normale downloader della skill è compatibile senza modifiche.
La rotta verifica namespace SCAN_ARCHIVE, job ID e hash prima di restituire
dati; non consente il download di archivi di conto o altri tipi privati.

Un errore di scrittura mantiene il risultato in RAM oltre il TTL. Un polling
successivo ritenta dopo almeno60 secondi senza ripetere Yahoo/Fusion. Il limite
è4 job trattenuti: a capacità raggiunta i nuovi job rispondono BUSY. Solo job
scaduti verificati vengono rimossi in modalità persistente. Nessun dato non
archiviato viene eliminato per fare spazio. Il TTL di15 minuti resta il limite
di riuso della scansione, non la durata dei file immutabili in Drive.

`/status.scan_archive_collector` espone conteggi e versione senza payload o
segreti. Questa fase non persiste lo storico prezzi, non recupera un job
interrotto prima della sua conclusione e non avvia scansioni autonome: restano
la pianificazione e il cron esistenti. Non vengono cambiate soglie o Mandato.

## Cache prezzi e cron — server1.7.54 / gateway1.1.0

Prima del deploy aggiornare il codice Apps Script ad ArchiveGateway.gs1.1.0 e
pubblicare una nuova versione della distribuzione esistente. URL, chiave e
proprietà restano identici. Il nuovo protocollo conserva gli oggetti immutabili
preesistenti e aggiunge operazioni cache firmate e prenotazione dei due slot
tecnici. Google owner deployment resta un passo manuale, non effettuabile dal
connettore Drive disponibile. Non confondere codice GitHub con codice pubblicato
nel deployment Apps Script.

La cache giornaliera OHLCV compressa è ripartita in32 bucket. Ogni lotto salva
al massimo2 bucket modificati e poi pubblica un manifest con compare-and-swap
e rilettura firmata. Timestamp originali, fonte, calendario, valuta e flags
sono conservati: il server ricalcola la freschezza contro la seduta attesa.
Niente interpolazioni, forward-fill o conversione di provvisori in correnti.
Lo schema cache e gli hash di policy/mappe impediscono il riuso incompatibile;
una semplice versione server nuova non invalida da sola il contenuto.

La parte compressa della cache è limitata a64MiB e1100 voci, con un massimo
di512KB per serie giornaliera e2MB per oggetto remoto. La RAM effettiva include
Python/calendari e resta sotto i memory guard esistenti400/460MB: non equivale
al solo limite della cache. Un risultato giornaliero non ancora salvato non
è eliminato per TTL/capienza: a saturazione si interrompe l'inserimento con
un errore esplicito. Intraday e cache ausiliarie restano sacrificabili.
Oggetti tecnici cache e manifest non generano una riga Registro a ogni lotto;
le ricevute degli archivi scanner continuano nel Registro originale. Nessuna
cancellazione automatica dei file immutabili Drive è introdotta; monitorarne
la dimensione nella manutenzione ordinaria.

Il cron già esistente che chiama /warm mantiene esattamente lo stesso URL.
La chiamata attiva in background anche la preraccolta tecnica dal lunedì al
venerdì alle09:20(EU) e16:00(US), Europe/Rome, con finestra15minuti. Gli altri
slot e la pianificazione ChatGPT restano invariati. La prenotazione firmata
su Google impedisce avvii duplicati tra tick/processi/riavvii. Un job interrotto
prima della conclusione non è certificato completo: il lease dura20minuti e
non viene inventato uno storico dello slot. Lo scanner usa il profilo tecnico
balanced; lo stato appreso effettivo e i gate vengono applicati dall'agente
ordinario, che può richiedere la propria selezione senza override dello stato.
La preraccolta non è esecuzione completa del controllo di conto/rischio e non
registra Esecuzioni COMPLETADO o un ticket approvato. Nessun LLM viene chiamato
dal cron e nessuna operazione eToro viene inviata.

Controllare `/status.price_cache_persistence` (READY, verified_entries),
`/status.cron_collector` (last_warm_call, slot, ARCHIVED/errore esplicito).
Durante il ripristino la cache può essere parziale: il warmer continua senza
sovrascrivere dati più recenti con copie salvate. La persistenza è asincrona:
i dati non ancora verificati al momento di un crash possono andare persi; non
promettere recupero di dati mai salvati. Il primo deploy richiede ancora un
riempimento, poiché la1.7.53 non esporta la propria RAM.

Collaudo: verificare il salvataggio su fonti reali, annotare il numero delle
serie verificate, riavviare realmente Render e verificare restored_entries e
la data di acquisizione preservata. Oggi25/10/2026 è domenica: la prima prova
dello slot automatico è lunedì26/10 alle09:20 Europe/Rome; un test sintetico
non va presentato come quel ciclo. Nessuna modifica al mandato r8 o al rischio.

### Prova live iniziale del25/10/2026

Gateway1.1.0 pubblicato dal proprietario; server1.7.54 distribuito con commit
45fe16522b99dfbc81ea903b713cdf476661762b. Il controllo live ha verificato
il salvataggio e la rilettura firmata delle prime2 serie reali. Questo aggiornamento
documentale avvia il redeploy controllato per verificare il recupero dal manifest:
non modifica il codice e non attesta da solo la riuscita del riavvio. La prova
va completata confrontando restored_entries, barre e timestamp della fonte
prima/dopo. La raccolta dello slot feriale resta da osservare nel giorno previsto.

Il confronto live ha rilevato che il payload canonico del server contiene
`history`, mentre l'adattatore iniziale cercava `bars`. Il server1.7.55 /
price_cache_store1.0.1 corregge esclusivamente tale validazione. I blob già
salvati non cambiano e restano recuperabili. Un nuovo test usa direttamente
_build_payload del server per verificare il contratto reale di serializzazione.

## Collettore autonomo — gateway1.2.0, server1.7.56

Stato: codice e test predisposti; nessun collaudo live del nuovo collettore
è ancora dimostrato. La cache prezzi1.1.0 resta compatibile.

Prima del merge/deploy del server:

1. Sostituire tutto Codice.gs con ArchiveGateway.gs di questa revisione.
2. Salvare; Gestisci deployment → matita → Nuova versione → Esegui deployment.
3. Conservare URL e proprietà esistenti; nessuna nuova credenziale richiesta.

Il file privato `etoro-learning-bootstrap-v1.json` nella cartella archivio
lega l'identità del conto e l'hash della sorgente nel Registro originale.
Non caricarlo in GitHub. Il gateway legge i chunk esistenti in Registro!X,
il client valida parti, hash e formato prima di inizializzare lo stato.
Se fonte, identità o permessi non coincidono, il collettore si ferma.

Il cron esistente chiama `/warm`: ogni chiamata può avviare fino a quattro
piccoli blocchi del collettore in background, al massimo una volta al minuto
per processo. Non crea nuovi cron né cambia i cinque slot dell'agente.
Le fonti eToro sono una allowlist di GET più le sole anteprime read-only
di idoneità/costi. Non esistono chiamate di esecuzione, LLM o API a pagamento.

Blocchi: verifica conto, import archivi scanner già salvati, metadati/identità
in gruppi di quattro strumenti, ingresso shadow con quote/costi/FX/benchmark,
chiusura alla seduta pianificata e pagine dello storico di200 operazioni.
Mercato chiuso, prezzo/costo mancante e attesa della seduta restano PENDING
con motivo, senza prezzi inventati né consumo dei tentativi per errori.

Ogni blocco è preceduto da checkpoint firmato e riletto, con lease10minuti
e massimo3 tentativi per errore/interruzione. Oggetti grezzi immutabili,
ID stabili e compare-and-swap impediscono avanzamenti non verificati;
se una scrittura fallisce, gli effetti parziali del blocco non avanzano.
Ritornano in coda soltanto i blocchi incompleti. Gli esiti shadow e quelli
reali restano separati; senza prova ticket/esecuzione non si attribuisce
il profitto all'agente. Una copertura storica non certificata resta tale.

Lo stato appreso per scope diventa utilizzabile soltanto dopo pubblicazione
e readback nel Registro originale. Il motore mensile esistente mantiene
i requisiti sui campioni; nessun cambiamento di rischio o autorizzazioni.
Gli oggetti hanno il limite tecnico di2MB; raggiungerlo blocca l'avanzamento
con errore esplicito, senza cancellare dati. L'archivio immutabile cresce:
questa revisione non implementa la cancellazione dello storico.

Collaudo dopo deploy: osservare `/status.learning_collector` fino a READY,
checkpoint_verified=true e account_verified=true; verificare import del
conteggio coorti e ledger preesistenti e lo stesso head dopo riavvio reale.
READY può includere task BLOCKED: controllare pending_tasks, blocked_tasks
e issues; non è certificazione di un ciclo operativo completo o di profitto.
La prima entrata e uscita shadow reali richiedono mercati aperti e la seduta
prevista. I test sintetici non costituiscono campioni d'apprendimento.
