# ctxgate-proxy: local LLM context gateway
# Multi-stage build: slim runtime image

FROM python:3.11-slim AS runtime

WORKDIR /app

# System deps (libpq for asyncpg)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY proxy/ proxy/
COPY worker/ worker/
COPY dashboard/ dashboard/
COPY schema/ schema/
COPY config.example.yaml config.yaml
COPY Makefile .

# Non-root user
RUN useradd -m ctxgate
USER ctxgate

# Expose ports: proxy=9201, dashboard=9202
EXPOSE 9201 9202

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9201/health')" || exit 1

# Entry: start proxy (worker + dashboard can be started separately)
CMD ["python", "-u", "proxy/app.py"]
