FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS python-deps
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
RUN pip wheel --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim AS runtime
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
ENV FRONTEND_DIR=/app/frontend/dist
RUN pip install --no-deps . && mkdir -p /var/lib/paurubot/backups && chown -R paurubot /var/lib/paurubot
USER paurubot
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "paurubot"]
CMD ["api"]
