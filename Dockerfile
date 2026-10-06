# syntax=docker/dockerfile:1.7

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
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home /app paurubot
WORKDIR /app
COPY --from=python-deps /wheels /wheels
RUN pip install --no-index --find-links=/wheels /wheels/* && rm -rf /wheels
COPY --chown=paurubot:paurubot pyproject.toml alembic.ini README.md ./
COPY --chown=paurubot:paurubot migrations ./migrations
COPY --chown=paurubot:paurubot paurubot ./paurubot
RUN pip install --no-deps . && mkdir -p /var/lib/paurubot/backups && chown -R paurubot /var/lib/paurubot
USER paurubot
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "paurubot"]
CMD ["api"]
