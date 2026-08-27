FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/.cache/huggingface \
    SENTENCE_TRANSFORMERS_HOME=/data/.cache/sentence-transformers \
    YTSCRAPER_DATA_DIR=/data

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY config/ ./config/

RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["python", "-m", "src.cli", "server", "--host", "0.0.0.0", "--port", "8080"]