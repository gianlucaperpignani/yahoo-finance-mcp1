# yahoo-finance-mcp1
# Sprint 2 — release 1.7.49

Policy quantitative e parametri discovery sono distinti in `risk_policy.json`
e `screening_config.json`. `release_contract.json` ne fissa gli SHA e il pin
del pacchetto rischio esterno 1.3.7; il motore non viene duplicato qui.
I valori finanziari r6 restano invariati. Il Mandato originale resta autorità.

`/status` e `get_server_status` espongono `runtime_manifest`: hash effettivi
di codice/mappe, commit Render, policy e versioni delle dipendenze installate.
Il manifest di runtime non prova il completamento di un ciclo né la consegna.
L'archivio per ciclo e il recupero scritture appartengono alle skill aggiornate:
pipeline1.5.0, audit_bundle1.0.0, registry_outbox1.0.0. Gli artefatti privati
restano nel Registro originale con backup tecnico compresso su Drive;
non caricare conto, bundle o credenziali nella repo pubblica.

Verifica locale: `WARMER=0 python -m unittest discover -s tests -v`.
La CI esegue gli stessi test su push e pull request. Le due fixture storiche
live non presenti sono esplicitamente saltate; i test restanti sono offline.
Render continua a usare la sua modalità attuale di deploy: la CI da sola non
impedisce un auto-deploy già avviato. Pubblicare su main solo dopo test e review.

`scan_cycle_candidates` coordina Level A e Fusion in background e applica un
budget globale di dieci nuovi candidati prima della chat. I fallimenti delle
singole sorgenti restano espliciti; COMPLETE non autorizza ticket.

`adaptive_config.json` fissa il protocollo mensile: 30 coorti di training,
10 di validazione e 10 shadow realmente successive al congelamento. Le coorti
sono complete, non sovrapposte e senza dati futuri. `adaptive_method.py`
confronta profili di ranking, benchmark e drawdown senza cambiare soglie di
rischio. Uno stato proposto diventa efficace solo dopo persistenza e rilettura.
`outcome_ledger.py` separa copie, operazioni attribuite e risultati sconosciuti;
commissioni con convenzione non attestata restano non valutabili.
`event_evidence.py` richiede fatti verificati e provenienza delle fonti, non il
solo nome dell'editore. Il Mandato corrente r8 autorizza l'evoluzione del metodo,
mentre ogni operazione reale continua a richiedere conferma dell'utente.


Release1.7.49 integra il download compresso delle opportunità, il collettore
shadow con richieste di acquisizione riprendibili, scope DISCOVERY per il ranking,
attribuzione ticket/esecuzioni/uscite e KPI per categoria. La calibrazione resta
separata dalla probabilità originale; costi o identificativi ignoti non vengono
inventati. registry_formula_contract.json registra gli hash delle269 formule
osservate; formula_contract.py controlla anche21 soglie del testo master.
Il test pianificato reale resta distinto dalla validazione del software.
