# syntax=docker/dockerfile:1.7
# ------------------------------------------------------------------
# Pronaos production image — multi-stage, non-root, slim.
# ------------------------------------------------------------------

ARG PYTHON_VERSION=3.12-slim-bookworm

# ---------- Stage 1: builder ----------
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip wheel --no-deps --wheel-dir /wheels .

# ---------- Stage 2: runtime ----------
FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PRONAOS_HOST=0.0.0.0 \
    PRONAOS_PORT=8080

RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 pronaos \
    && useradd  --system --uid 1000 --gid pronaos --home /app --shell /usr/sbin/nologin pronaos

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

USER pronaos

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
    sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status==200 else sys.exit(1)"

ENTRYPOINT ["uvicorn", "pronaos.main:app", "--host", "0.0.0.0", "--port", "8080"]
