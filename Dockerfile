# Node and Python match what CI tests (SEC-F24): Node 22 builds the frontend, Python 3.14 runs.
# The frontend is static files the same on every CPU: build it once, natively, instead of under
# emulation for each platform (arm64 npm ci under QEMU is slow and can hang a release).
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim AS python-deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.lock .
# Only the pinned, hash-checked set (SEC-F24): a changed or substituted file fails the build.
RUN pip wheel --require-hashes --wheel-dir /wheels -r requirements.lock

FROM python:3.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
# pg_dump must match the PostgreSQL 16 server, so the client comes from the PGDG repository.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg tini libfribidi0 \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        | gpg --dearmor -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg \
    && . /etc/os-release \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app paurubot
WORKDIR /app
COPY --from=python-deps /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels
COPY --chown=paurubot:paurubot pyproject.toml alembic.ini README.md ./
COPY --chown=paurubot:paurubot migrations ./migrations
COPY --chown=paurubot:paurubot paurubot ./paurubot
COPY --from=frontend --chown=paurubot:paurubot /build/dist ./frontend/dist
ENV FRONTEND_DIR=/app/frontend/dist PYTHONPATH=/app
# The code runs from /app as it is. Installing it with pip would fetch an unpinned, unhashed
# build backend (setuptools) from PyPI; the `paurubot` command is a wrapper instead.
RUN printf '#!/bin/sh\nexec python -m paurubot "$@"\n' > /usr/local/bin/paurubot \
    && chmod 755 /usr/local/bin/paurubot \
    && mkdir -p /var/lib/paurubot/backups && chown -R paurubot /var/lib/paurubot
USER paurubot
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "paurubot"]
CMD ["api"]
