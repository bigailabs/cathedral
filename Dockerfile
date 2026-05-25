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

# Post-PR2 bootstrap: SAT lane only. The card_definitions seed /
# load-eval-spec / archive-cards bootstrap steps were removed when the
# card-era endpoints were stripped — there are no cards to seed, the
# only accepted card_id is `synthetic_boolean_v1` (the SAT task family
# marker), and registrations never read card_definitions.
CMD ["sh", "-c", "\
  echo '[startup] serve --db '${CATHEDRAL_DB_PATH}' --port '${PORT:-8080} && \
  cathedral-publisher serve --db ${CATHEDRAL_DB_PATH} --port ${PORT:-8080} --host 0.0.0.0 \
"]
