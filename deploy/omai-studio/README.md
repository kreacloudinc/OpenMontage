# Agente di regia per OMAI Studio

Questa cartella contiene l'integrazione fra OpenMontage e
[OMAI Studio](https://github.com/kreacloudinc/app_videomai_2026), la video factory che gira
su `studio.omai.cloud`.

Non modifica OpenMontage: aggiunge soltanto un gateway websocket e l'immagine che lo serve,
cosi' il fork resta allineabile con il repository di origine.

| File | Cosa fa |
|---|---|
| `gateway.py` | Websocket davanti a Claude Code. Un progetto = una cartella = una sessione. |
| `Dockerfile` | Immagine con Python, Node, Chromium, ffmpeg e la CLI di Claude Code. |

## Come si incastra

```
pannello OMAI Studio  --(websocket, sessione gia' autenticata)-->  gateway :8010
                                                                        |
                                                            Claude Code nella
                                                            cartella del progetto
                                                                        |
                                              /projects (volume)   /factory (sola lettura)
```

Il container **non ha porte pubbliche e nessuna label Traefik**: sta sulla rete interna.
L'unico modo per parlargli e' passare dal pannello, che ha gia' verificato chi sei e a
quale spazio di lavoro appartieni. La cartella di lavoro la decide il pannello dagli slug
di tenant e progetto: non arriva mai dal browser.

## Configurazione

| Variabile | Default | A cosa serve |
|---|---|---|
| `OM_PROJECTS` | `/projects` | Radice delle cartelle di progetto. |
| `OM_CLAUDE_FLAGS` | *(vuoto)* | Opzioni passate alla CLI. Vedi sotto. |
| `OM_CLAUDE_MODEL` | *(vuoto)* | Forza un modello invece del default della CLI. |
| `OM_MAX_CONCURRENT` | `1` | Turni in parallelo sull'intero container. |
| `OM_TURN_TIMEOUT_S` | `1800` | Oltre questo, il turno viene ucciso. |
| `ANTHROPIC_API_KEY` | — | Chiave dell'agente. In alternativa `CLAUDE_CODE_OAUTH_TOKEN`. |

### Sui permessi

`OM_CLAUDE_FLAGS` e' **vuoto di default**, quindi l'agente chiede conferma come farebbe
in un terminale.

La bozza di partenza di questo gateway cablava `--dangerously-skip-permissions`, cioe' un
agente che approva da solo qualunque comando. Il container e' isolato, e per una
produzione lunga le conferme sono scomode: e' una scelta legittima. Ma se e' il default,
la modalita' piu' pericolosa e' quella che ottieni quando non leggi la configurazione, e
un rischio va scelto, non ereditato.

Per allentarli, si scrive nella configurazione dello stack:

```
OM_CLAUDE_FLAGS=--dangerously-skip-permissions
```

`GET /healthz` riporta `conferme_disattivate`, cosi' si sa sempre in che modalita' gira
senza andare a leggere le variabili.

### Perche' un turno per volta

`OM_MAX_CONCURRENT=1` non e' prudenza generica. Ogni turno puo' aprire Chromium per i
render Remotion: su un N100 con 16 GB condivisi con Postgres, il pannello e il worker
ffmpeg, due produzioni in parallelo fanno cadere anche la Factory. Alzarlo ha senso solo
su una macchina piu' grande.

## Sessioni

Le sessioni sono salvate in `$OM_PROJECTS/.sessions.json`, non in memoria: riavviare il
container non azzera il filo del discorso. Nella bozza stavano in RAM, e dopo un riavvio
l'agente ripartiva da zero — i file del progetto c'erano ancora, il contesto no, e ce ne
si accorgeva solo dalle risposte che non tornavano.

## Costruzione

La CI (`.github/workflows/omai-studio-image.yml`) costruisce e pubblica
`ghcr.io/kreacloudinc/openmontage-gateway`. In locale:

```bash
docker build -f deploy/omai-studio/Dockerfile -t openmontage-gateway .
```

Il contesto e' la radice del repository, non questa cartella.
