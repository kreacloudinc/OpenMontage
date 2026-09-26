# Immagine dell'agente di regia per OMAI Studio.
#
# Si costruisce dalla radice del repository:
#   docker build -f deploy/omai-studio/Dockerfile -t openmontage-gateway .
#
# La costruzione la fa la CI di GitHub, non il server: su un N100 a 4 core, mettere
# insieme Node e Chromium sono decine di minuti di macchina ferma che non fa altro.
FROM python:3.12-slim

# Node serve al compositore Remotion, Chromium al suo rendering, ffmpeg al montaggio.
# I driver VAAPI danno a ffmpeg l'accelerazione dell'iGPU quando /dev/dri e' passato.
# Va la variante di "main": la -non-free non e' installabile su python:3.12-slim,
# dove il componente non-free non e' abilitato, e fa fallire il build.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg git \
        ffmpeg fonts-liberation \
        chromium \
        intel-media-va-driver \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get purge -y gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# Remotion cerca un Chromium proprio e se lo scaricherebbe a ogni avvio: gli si dice
# di usare quello di sistema.
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium \
    PUPPETEER_SKIP_DOWNLOAD=true \
    REMOTION_BROWSER_EXECUTABLE=/usr/bin/chromium \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN npm install -g @anthropic-ai/claude-code

WORKDIR /opt/openmontage

# Le dipendenze prima del codice: cosi' una modifica alle skill non invalida
# l'intero strato di installazione.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32" "websockets>=13"

COPY remotion-composer/package.json remotion-composer/package-lock.json* remotion-composer/
RUN cd remotion-composer && (npm ci --omit=dev || npm install --omit=dev)

COPY . .
COPY deploy/omai-studio/gateway.py /opt/openmontage/gateway.py

# I progetti stanno su un volume, il materiale della Factory arriva in sola lettura.
ENV OM_PROJECTS=/projects
RUN mkdir -p /projects /factory

EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=5).status==200 else 1)"

CMD ["uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "8010"]
