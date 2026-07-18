# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Keep Python lean and log-friendly inside containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
#
# EXTRA_CA_CERT (optional): filename of a PEM CA in the build context to trust
# at build time. Needed only when building behind a TLS-inspecting corporate
# proxy; harmless (a no-op) when left empty, so normal builds are unaffected.
ARG EXTRA_CA_CERT=""
COPY requirements.txt ${EXTRA_CA_CERT} ./
RUN if [ -n "$EXTRA_CA_CERT" ]; then \
        cp "$EXTRA_CA_CERT" /usr/local/share/ca-certificates/extra-ca.crt && \
        update-ca-certificates && \
        pip config set global.cert /etc/ssl/certs/ca-certificates.crt; \
    fi && \
    pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Data directory (mounted as a volume in compose; created here as a fallback).
RUN mkdir -p /app/data

# UDP telemetry ingress + HTTP/WebSocket dashboard.
EXPOSE 9876/udp
EXPOSE 8080/tcp

# Container-level healthcheck hits the app's /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=3).status==200 else 1)"

# uvicorn serves FastAPI; the UDP listener is started in the app lifespan.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
