# =============================================================================
# Backend image - one image, three targets.
#   builder : resolves wheels into a virtualenv
#   dev     : + dev deps, source mounted, hot reload
#   runtime : slim production image, non-root
# =============================================================================
ARG PYTHON_VERSION=3.11-slim-bookworm

# ------------------------------------------------------------------- builder
FROM python:${PYTHON_VERSION} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build toolchain needed by asyncpg / argon2-cffi / pymupdf wheels.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml ./
# Copy the package skeleton so the editable/PEP 517 build can resolve it.
COPY app/__init__.py app/__init__.py

# Install extras that the container actually needs. `local-ml` is intentionally
# excluded (torch); enable EMBEDDING_PROVIDER=sentence_transformers only on an
# image built with `--build-arg EXTRAS=...,local-ml`.
ARG EXTRAS="ai,parsers,s3,azure"
RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[${EXTRAS}]"

# ----------------------------------------------------------------- base-runtime
FROM python:${PYTHON_VERSION} AS base-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/opt/venv/bin:$PATH" \
    TESSERACT_CMD=/usr/bin/tesseract

# Runtime OS deps: libpq for psycopg, tesseract+poppler for OCR/PDF rasterising.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        tesseract-ocr \
        tesseract-ocr-eng \
        poppler-utils \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Non-root runtime user; storage mount point is writable for the local adapter.
RUN groupadd --gid 10001 cip \
    && useradd --uid 10001 --gid cip --create-home --shell /usr/sbin/nologin cip \
    && mkdir -p /var/lib/cip/storage /app \
    && chown -R cip:cip /var/lib/cip /app

WORKDIR /app

# ------------------------------------------------------------------------ dev
FROM base-runtime AS dev

RUN pip install --no-cache-dir \
      pytest pytest-asyncio pytest-cov pytest-mock asgi-lifespan faker \
      freezegun ruff mypy

COPY --chown=cip:cip . /app
RUN pip install --no-deps -e .

USER cip
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# -------------------------------------------------------------------- runtime
FROM base-runtime AS runtime

COPY --chown=cip:cip alembic.ini /app/alembic.ini
COPY --chown=cip:cip migrations /app/migrations
COPY --chown=cip:cip app /app/app
COPY --chown=cip:cip pyproject.toml /app/pyproject.toml
RUN pip install --no-deps --no-cache-dir -e .

USER cip
EXPOSE 8000 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=4).status==200 else 1)"

# Gunicorn + uvicorn workers in production; docker-compose overrides with a
# single uvicorn process for readable logs.
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--max-requests", "2000", \
     "--max-requests-jitter", "200", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
