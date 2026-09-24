# Private AI Assistant — container image (Linux servers, or Docker Desktop on a Mac).
# On a Mac Studio, run Ollama natively for GPU acceleration and point the container at it:
#   OLLAMA_BASE_URL=http://host.docker.internal:11434/v1   (treated as local: it is the same machine)
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data STORAGE_DIR=/storage DB_PATH=/storage/assistant.duckdb LOG_FORMAT=json

# Local OCR engine for scanned documents
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements.lock.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY data/qbo_sandbox ./data/qbo_sandbox
COPY pyproject.toml ./

# Run as an unprivileged user; data and state live on mounted volumes.
RUN useradd --create-home --uid 10001 assistant \
 && mkdir -p /data /storage && chown -R assistant /data /storage
USER assistant
ENV QBO_FIXTURE=/app/data/qbo_sandbox/sandbox_company.json

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ready || exit 1

# Single worker: the model router, conversation memory and caches are in-process state.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
