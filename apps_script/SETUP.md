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
