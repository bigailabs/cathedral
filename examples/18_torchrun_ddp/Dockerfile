FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

# Create non-root user (required by Cathedral)
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY pyproject.toml .

# Install Python dependencies (torch already in base image)
RUN pip install --no-cache-dir \
    transformers>=4.28.0 \
    accelerate>=1.6.0 \
    datasets>=3.5.1 \
    rich>=14.0.0 \
    python-dotenv>=1.0.1 \
    uvloop>=0.21.0 \
    torchft>=0.1.1 \
    huggingface-hub>=0.30.2 \
    sentencepiece>=0.2.0 \
    protobuf>=3.20.0

# Copy application code
COPY --chown=appuser:appuser src/ ./src/
COPY --chown=appuser:appuser train.py .
COPY --chown=appuser:appuser run.sh .
COPY --chown=appuser:appuser entrypoint.py .

# Set PYTHONPATH so the package is importable
ENV PYTHONPATH=/app/src

USER appuser

EXPOSE 8000

CMD ["python", "entrypoint.py"]
