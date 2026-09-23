# — ms-duke-je-gocoll-agent-service —
# Multi-stage build - Python 3.14 Alpine builder + slim runtime, non-root user.
# Build context: repo root (self-contained)
# Runs the GOCOLL Weekly JE executor on port 8018.

# — Stage 1: builder —
FROM python:3.14.6-alpine3.24 AS builder

# Build-only deps. Alpine/musl has no wheel for every pinned package, so the
# toolchain (build-base, libffi/openssl headers, cargo for Rust extensions) has
# to be present here; this whole stage is discarded, so none of it ships.
RUN apk update && \
    apk upgrade --no-cache && \
    apk add --no-cache \
    build-base \
    cargo \
    ca-certificates \
    git \
    libffi-dev \
    openssl-dev \
    postgresql-dev

WORKDIR /build

# Install Python deps into an isolated prefix that is copied into the runtime
# stage, so build tooling and the registry credentials never reach the final
# image. Credentials are mounted as BuildKit secrets, not build args.
# INSECURE_GIT is retained for pipeline compatibility (builds behind a
# TLS-inspecting proxy such as Zscaler); it is unused now that all deps resolve
# from the Artifactory index rather than git.
ARG INSECURE_GIT=false

# Pass --build-arg CACHE_BUST=$(date +%s) to force pip re-install even when
# requirements.txt is unchanged (e.g. a floating common release moved).
ARG CACHE_BUST=3
COPY requirements.txt .
RUN --mount=type=secret,id=jfrog_user \
    --mount=type=secret,id=jfrog_password \
    pip install --no-cache-dir --upgrade --prefix=/install \
    --extra-index-url "https://$(cat /run/secrets/jfrog_user):$(cat /run/secrets/jfrog_password)@eyctpeu.jfrog.io/artifactory/api/pypi/finance-da... -r requirements.txt

# — Stage 2: runtime —
FROM python:3.14.6-alpine3.24 AS runtime

# Runtime deps only:
# poppler-utils   - pdf2image (pdftoppm binary)
# postgresql-libs - pg client library (no headers/compiler)
# curl            - HEALTHCHECK below
# ca-certificates - TLS to Azure OpenAI / Blob Storage
RUN apk update && \
    apk upgrade --no-cache && \
    apk add --no-cache \
    ca-certificates \
    curl \
    poppler-utils \
    postgresql-libs

COPY --from=builder /install /usr/local

WORKDIR /app

# — GOCOLL Pipeline Agent (port 8018) —
COPY agent/src/ /app/agent/src/
COPY agent/config/ /app/agent/config/

# — Service launcher —
COPY serve.py /app/serve.py
# bootstrap_secrets now ships with ms-duke-je-common (installed via requirements.txt)

# A2A agent port
EXPOSE 8018

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD sh -c 'curl -sf "http://localhost:${GOCOLL_AGENT_PORT:-8018}/healthz"' || exit 1

# serve.py starts the executor.
CMD ["python", "/app/serve.py"]