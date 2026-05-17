FROM python:3.11-slim

WORKDIR /app

# System deps for Bittensor SDK + crypto
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libssl-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# Persistent SQLite for the publisher's submission ledger.
# Railway provides volume mounts via the dashboard rather than VOLUME directives;
# the publisher writes to whatever CATHEDRAL_DB_PATH points at.
RUN mkdir -p /data
ENV CATHEDRAL_DB_PATH=/data/publisher.db

EXPOSE 8080

# Force unbuffered Python output so Railway captures stack traces if the
# publisher crashes at startup. Without this, Python buffers stdout/stderr
# until the process flushes, which never happens on a hard crash.
ENV PYTHONUNBUFFERED=1
ENV PYTHONFAULTHANDLER=1

# Bootstrap on every container start:
# 1. seed-cards: idempotent INSERT-or-UPDATE of the launch card_definitions
#    rows (eu-ai-act, us-ai-eo, uk-ai-whitepaper, singapore-pdpc,
#    japan-meti-mic). Cheap; runs even if the rows already exist.
# 2. load-eval-spec: pulls real per-card content from the public
#    cathedral-eval-spec GitHub repo and updates each row. Idempotent.
#    Failure is non-fatal (logs to stderr, container still starts); we'd
#    rather serve placeholder content than refuse to start.
# 3. serve: start uvicorn + the FastAPI app + background eval orchestrator.
#
# The earlier archive-cards step (which marked us-ai-eo / uk-ai-whitepaper /
# singapore-pdpc / japan-meti-mic as status='archived') is intentionally
# absent: those cards reopened in this multi-jurisdiction launch and
# need to be active again. The archive-cards CLI still exists for future
# deprecations; the production startup just no longer invokes it.
CMD ["sh", "-c", "\
  echo '[startup] seed-cards' && \
  cathedral-publisher seed-cards --db ${CATHEDRAL_DB_PATH} && \
  echo '[startup] load-eval-spec' && \
  cathedral-publisher load-eval-spec --db ${CATHEDRAL_DB_PATH} || echo '[startup] load-eval-spec failed; continuing with placeholder content' && \
  echo '[startup] serve --db '${CATHEDRAL_DB_PATH}' --port '${PORT:-8080} && \
  cathedral-publisher serve --db ${CATHEDRAL_DB_PATH} --port ${PORT:-8080} --host 0.0.0.0 \
"]
