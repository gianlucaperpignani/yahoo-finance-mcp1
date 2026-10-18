# yahoo-finance-mcp1
# Sprint 1 — release 1.7.46

Policy quantitative e parametri discovery sono distinti in `risk_policy.json`
e `screening_config.json`. `release_contract.json` ne fissa gli SHA e il pin
del pacchetto rischio esterno 1.3.5; il motore non viene duplicato qui.
I valori finanziari r6 restano invariati. Il Mandato originale resta autorità.

`/status` e `get_server_status` espongono `runtime_manifest`: hash effettivi
di codice/mappe, commit Render, policy e versioni delle dipendenze installate.
Il manifest di runtime non prova il completamento di un ciclo né la consegna.
L'archivio per ciclo e il recupero scritture appartengono alle skill aggiornate:
pipeline1.3.0, audit_bundle1.0.0, registry_outbox1.0.0. Gli artefatti privati
restano nel Registro originale con backup tecnico compresso su Drive;
non caricare conto, bundle o credenziali nella repo pubblica.

Verifica locale: `WARMER=0 python -m unittest discover -s tests -v`.
La CI esegue gli stessi test su push e pull request. Le due fixture storiche
live non presenti sono esplicitamente saltate; i test restanti sono offline.
Render continua a usare la sua modalità attuale di deploy: la CI da sola non
impedisce un auto-deploy già avviato. Pubblicare su main solo dopo test e review.

