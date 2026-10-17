# Build from the repository root:  docker build -f infra/docker/Dockerfile -t parsec .
# Run:                             docker run --env-file .env -p 8000:8000 parsec
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

RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
