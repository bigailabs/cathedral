# Cathedral v4 thin publisher — slim runtime image.
#
# Python 3.11 to match the venv that the gates pass under (bittensor-wallet 4.1.0
# ships manylinux wheels for cp311; the slim/Debian base is glibc, which those
# wheels target — no rust toolchain needed at build time).
FROM python:3.11-slim AS base

# Faster, quieter, no .pyc clutter; unbuffered logs for Railway.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first (layer cache: deps change rarely vs source).
COPY deploy/requirements.txt /app/deploy/requirements.txt
RUN pip install --no-cache-dir -r /app/deploy/requirements.txt

# Copy only what the publisher runtime needs (scaffold package).
COPY scaffold /app/scaffold

# Non-root runtime user; owns the working dir so the SQLite DB is writable.
RUN useradd --create-home --uid 10001 cathedral && chown -R cathedral:cathedral /app
USER cathedral

# DB lives in the working dir by default; mount a volume here for persistence.
ENV CATHEDRAL_DB_PATH=/app/data/publisher.db
RUN mkdir -p /app/data

# Railway provides $PORT; default 8000 for local runs.
ENV PORT=8000
EXPOSE 8000

# Healthcheck hits the same /health the railway.toml healthcheck uses.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url=f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/health\"; \
sys.exit(0 if urllib.request.urlopen(url,timeout=4).status==200 else 1)"

# Shell form so $PORT expands at runtime.
CMD uvicorn scaffold.publisher.server:app --host 0.0.0.0 --port ${PORT}
