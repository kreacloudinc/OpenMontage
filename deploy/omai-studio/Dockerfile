# Agente di regia per OMAI Studio.
#
# Immagine: ghcr.io/kreacloudinc/openmontage-gateway
# Contesto di build: la radice della repo (`docker build -f deploy/omai-studio/Dockerfile .`)
#
# Si costruisce dalla CI e non sul server: SANDONATO-EXP e' un N100, e Node piu'
# Chromium sono decine di minuti di macchina ferma mentre il pannello non risponde.
#
# Chromium arriva da Debian e NON da `npx remotion browser ensure`: quello scarica
# centinaia di megabyte al primo render, cioe' dentro il primo turno di una persona
# che sta aspettando. Meglio pesare nell'immagine che nell'attesa.

FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    OM_PROJECTS=/projects \
    OM_OPENMONTAGE=/opt/openmontage \
    OM_FACTORY=/factory \
    # Remotion e Puppeteer cercano un browser da scaricare se non gli si dice
    # dove ne hanno gia' uno.
    PUPPETEER_SKIP_DOWNLOAD=1 \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    REMOTION_BROWSER_EXECUTABLE=/usr/bin/chromium \
    CHROME_PATH=/usr/bin/chromium

RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv \
      ffmpeg \
      chromium \
      fonts-liberation fonts-dejavu-core fonts-noto-color-emoji \
      git ca-certificates tini \
      # L'iGPU dell'N100: senza il driver VA-API il render cade in software
      # senza dire perche', e su quattro core si sente.
      intel-media-va-driver-non-free libva2 libva-drm2 vainfo \
    && rm -rf /var/lib/apt/lists/*

ENV LIBVA_DRIVER_NAME=iHD

# La CLI dell'agente. Versione fissata: un aggiornamento che cambia il formato di
# `--output-format stream-json` spegne la chat, e succederebbe a un redeploy
# qualsiasi senza che nessuno abbia toccato niente.
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
    && npm cache clean --force

WORKDIR /opt/openmontage

# Prima le dipendenze, poi il sorgente: cosi' un cambio di codice non rifa'
# l'installazione, che e' la parte lenta.
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY remotion-composer/package.json remotion-composer/package-lock.json* remotion-composer/
RUN cd remotion-composer && npm ci --omit=dev || npm install --omit=dev

COPY . /opt/openmontage

# Il gateway sta nella repo, ma gira come programma suo.
WORKDIR /
RUN mkdir -p /projects /factory

EXPOSE 8010

# tini come pid 1: l'agente apre Chromium e ffmpeg, e senza un init che raccolga
# i figli il container accumula zombie fino a restare senza processi.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "/opt/openmontage/deploy/omai-studio/gateway.py"]

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=5).status==200 else 1)"
