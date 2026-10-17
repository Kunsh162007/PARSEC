# Build from the repository root:  docker build -f infra/docker/Dockerfile -t fineprint .
# Run:                             docker run --env-file .env -p 8000:8000 fineprint
# Hosts that set $PORT (Render, Cloud Run) are followed. Measured peak memory: about 175 MB for the largest request.
FROM python:3.11-slim

WORKDIR /app

# Node runs the same rules engine as the browser for /api/v1/diligence/* (packages/review/rules_runner.js).
RUN apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY apps ./apps
COPY packages ./packages
COPY config ./config
COPY benchmarks ./benchmarks
COPY scripts ./scripts
COPY demo ./demo

# The OCR engine for scanned PDFs, served by this server (pinned, SHA-512 checked).
RUN python scripts/fetch_ocr.py

# Deal folders live in DEALS_DIR; the practice deal is copied in at start-up so a fresh host is not empty.
ENV DEALS_DIR=/tmp/deals PORT=8000
RUN useradd --create-home --uid 1000 app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "mkdir -p \"$DEALS_DIR\" && cp -rn /app/demo/deals/. \"$DEALS_DIR\"/ && exec uvicorn apps.api.main:app --host 0.0.0.0 --port \"$PORT\""]
