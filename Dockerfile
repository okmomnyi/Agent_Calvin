# AgentOS — one image, two processes (api + bot). See docker-compose.yml.
#
# The api (kernel + APScheduler) and the bot (Telegram long-poll) are the SAME image run with
# different commands, exactly as they are two PM2 processes on a bare droplet: if one dies the
# other keeps working. Building them separately would let their dependencies drift apart.

# ============================================================== builder
# Wheels are built here and only the resulting venv is copied into the runtime stage, so the
# compilers never ship.
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Debian's mirrors are plain HTTP by default, and a captive/zero-rated mobile network (this is
# built on Safaricom) transparently hijacks HTTP — apt then dies on "Redirection loop
# encountered" pointing at the carrier's portal instead of the mirror. HTTPS can't be
# intercepted that way. Retries cover the ordinary flakiness of a mobile link.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copied alone so this layer is cached until requirements.txt itself changes — editing a skill
# must not trigger a full reinstall.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Optional, heavy (torch pulls ~2GB): semantic vault embeddings + droplet-side transcription
# for Telegram voice notes and lecture capture. Off by default — without them the vault falls
# back to the dependency-free hashing embedder and voice notes simply aren't transcribed.
#   docker compose build --build-arg WITH_ML=1
ARG WITH_ML=0
RUN if [ "$WITH_ML" = "1" ]; then \
        pip install sentence-transformers faster-whisper pytesseract ; \
    fi

# ============================================================== runtime
FROM python:3.13-slim AS runtime

# postgresql-client: `manage.py backup` shells out to pg_dump and fails loudly without it.
# tesseract-ocr: OCR for note images in the study vault (only used when pytesseract is present).
# tzdata: rules like "no explicit lyrics before 8am" resolve against Calvin's timezone.
# HTTPS + retries: see the builder stage — plain HTTP gets hijacked on a captive mobile network.
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g; s|http://security.debian.org|https://security.debian.org|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        postgresql-client \
        tesseract-ocr \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY --from=builder /opt/venv /opt/venv

# UID 1000 matches the first non-root user on a stock Ubuntu droplet, so bind-mounted data/
# and logs/ stay writable without chowning anything on the host.
RUN useradd --create-home --uid 1000 agentos
WORKDIR /app

COPY --chown=agentos:agentos . .

# Created here so the directories exist and are owned correctly even before any volume is
# mounted over them (an empty bind mount would otherwise land root-owned).
RUN mkdir -p data logs && chown -R agentos:agentos data logs
USER agentos

EXPOSE 8000

# urllib rather than curl: the check shouldn't be a reason to add a package to the image.
# /api/health reports scheduler + DB + skills, so this fails if the kernel is up but broken.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else sys.exit(1)"]

CMD ["python", "-m", "uvicorn", "kernel.app:app", "--host", "0.0.0.0", "--port", "8000"]
