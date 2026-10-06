#!/usr/bin/env bash
# Development only, before the first release: rebuild migration 0001 from the models.
set -euo pipefail
cd "$(dirname "$0")/.."
DB=${REGEN_DB:-postgresql+asyncpg://postgres@127.0.0.1/paurubot_regen}
psql -h 127.0.0.1 -U postgres -qc "DROP DATABASE IF EXISTS paurubot_regen" -c "CREATE DATABASE paurubot_regen"
rm -f migrations/versions/0001_*.py
mv migrations/versions/0002_ledger_immutability.py /tmp/0002.py
DATABASE_URL=$DB alembic revision --autogenerate -m "baseline schema" --rev-id 0001 >/dev/null
mv /tmp/0002.py migrations/versions/0002_ledger_immutability.py
DATABASE_URL=$DB alembic upgrade head >/dev/null
ruff check -q --fix migrations && ruff format -q migrations
echo "baseline regenerated"
