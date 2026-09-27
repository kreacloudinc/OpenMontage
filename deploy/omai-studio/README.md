# L'agente di regia di OMAI Studio

Questa cartella costruisce `ghcr.io/kreacloudinc/openmontage-gateway`, il container
`openmontage` dello stack **app_videomai_2026** su SANDONATO-EXP.

OMAI Studio e' una coda di produzione con un pannello: riceve una sceneggiatura, la
spezza in job, chiama i provider, monta con ffmpeg e tiene il conto di cosa e'
costato. Per generare una clip il pannello basta e avanza. Per le produzioni che
vanno **costruite** — piu' scene, un cast coerente, un montaggio con una struttura —
il pannello apre una chat, e dall'altra parte c'e' questo.

## Il contratto con il pannello

Piccolo di proposito: e' l'unica cosa su cui le due parti devono essere d'accordo.

```
  ws://openmontage:8010/ws?project=<slug-spazio>__<slug-progetto>

  pannello -> {"type": "message", "text": "..."}
  gateway  -> {"type": "text",   "text": "..."}    quello che dice l'agente
              {"type": "log",    "text": "..."}    strumenti, costi, guai
              {"type": "status", "status": "..."}  una riga di stato
```

Nessuna porta pubblica e nessuna label Traefik: si raggiunge solo dalla rete interna
dello stack, e chi arriva e' gia' passato dalla sessione del pannello
(`app/routers/chat.py` in `app_videomai_2026`).

## Cosa fa, in ordine

1. **Controlla il nome del progetto.** Studio lo ricava da slug che ha gia'
   verificato contro la sessione, ma questa porta e' raggiungibile da qualunque cosa
   stia sulla rete interna. Un nome che uscisse da `/projects` farebbe leggere a una
   produzione il lavoro di un'altra: si valida contro un pattern, e poi si risolve il
   percorso e si controlla che sia ancora figlio diretto della cartella dei progetti.
2. **Prepara la cartella.** Tre collegamenti e una guida:

   | Nel workspace | Punta a | Perche' |
   |---|---|---|
   | `openmontage/` | `/opt/openmontage` | la piattaforma, senza copiarla per progetto |
   | `factory/` | `/factory` (sola lettura) | il materiale gia' prodotto da Studio |
   | `.claude/` | `/opt/openmontage/.claude` | skill e strumenti anche fuori dal checkout |
   | `CLAUDE.md` | — | dice all'agente di leggere `AGENT_GUIDE.md` prima di rispondere |

3. **Esegue un turno per volta** e trasmette quello che succede.

## Le tre scelte che contano

**Un turno per volta** (`OM_MAX_CONCURRENT`, default 1). Un render Remotion apre
Chromium; due, su un N100 con 16 GB, si portano via il pannello. Chi arriva secondo
aspetta, e gli viene detto che sta aspettando invece di vedere una chat muta.

**Un turno che smette di rispondere viene ucciso** (`OM_TURN_TIMEOUT_S`, default 30
minuti). L'agente parte in un gruppo di processi suo proprio perche' al timeout se ne
vada l'albero intero: un Chromium orfano tiene un giga per tutta la vita del
container.

**La sessione si riprende con `--resume`, non con `--continue`.** `--continue`
prende la conversazione piu' recente in quella cartella, che e' la stessa cosa
finche' un turno non viene ritentato — e da li' in poi e' un'altra cosa senza dirlo.
L'id sta in `.omai/sessione.json` sul volume del progetto: in memoria, ogni
redeploy riporterebbe l'agente al primo turno e se ne accorgerebbe solo la persona
dall'altra parte.

## Variabili

| Variabile | Default | Cosa fa |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | senza, nessun turno parte |
| `OM_PROJECTS` | `/projects` | volume delle produzioni (scrivibile) |
| `OM_FACTORY` | `/factory` | materiale di Studio, montato in sola lettura |
| `OM_OPENMONTAGE` | `/opt/openmontage` | la piattaforma dentro l'immagine |
| `OM_CLAUDE_FLAGS` | vuoto | argomenti aggiuntivi per la CLI |
| `OM_MAX_CONCURRENT` | `1` | turni in parallelo |
| `OM_TURN_TIMEOUT_S` | `1800` | quanto si aspetta prima di fermare un turno |
| `OM_PORT` | `8010` | porta del gateway |

### Su `OM_CLAUDE_FLAGS`

Da un websocket **nessuno puo' rispondere a una richiesta di permesso**. Con la
variabile vuota l'agente si ferma al primo strumento che ne chiede uno, e da fuori
sembra rotto invece che prudente. Il gateway lo dice in chat appena ci si collega,
ma vale la pena saperlo prima: per lasciarlo agire si mette

```
OM_CLAUDE_FLAGS=--dangerously-skip-permissions
```

fra le variabili dello stack in Portainer. E' una scelta da fare consapevolmente —
l'agente scrive nel volume del progetto e legge il materiale della Factory in sola
lettura, ma dentro quel perimetro fa quello che ritiene.

## Quello che l'agente non puo' fare da qui

Accodare job sui provider a pagamento della Factory. Quelli passano dal ledger di
Studio — si stima, si prenota, si riconcilia — ed e' il motivo per cui il progetto
esiste: una spesa che nascesse qui sarebbe una riga che non compare in nessun conto.
Il materiale di Studio e' montato in sola lettura anche per questo.

## Costruire e provare

```bash
# dalla radice della repo
docker build -f deploy/omai-studio/Dockerfile -t openmontage-gateway:prova .

docker run --rm -p 8010:8010 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -v "$PWD/.tmp-projects:/projects" \
  openmontage-gateway:prova

curl -s localhost:8010/healthz
```

Il gateway gira anche fuori dal container, per lavorarci sopra:

```bash
pip install -r requirements.txt
OM_PROJECTS=/tmp/progetti OM_OPENMONTAGE="$PWD" python deploy/omai-studio/gateway.py
```

I test non hanno bisogno ne' della CLI ne' della rete:

```bash
pytest tests/deploy -v
```

## La CI

`.github/workflows/omai-gateway.yml` costruisce e pubblica su ogni push a `main` e
sui tag `omai-v*`. Nessun filtro sui percorsi, ed e' voluto: il server fa
`docker compose pull` e prende `latest`, e un filtro che lasciasse fuori un file
che conta servirebbe codice vecchio senza che niente lo dica.

Sul server non si costruisce mai:

```bash
docker compose pull openmontage && docker compose up -d openmontage
```

Per fissare una versione invece di seguire `latest`, si mette `OM_IMAGE_TAG` fra le
variabili dello stack.
