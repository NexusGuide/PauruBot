#!/usr/bin/env bash
# Same gates as CI: compile, lint, format, types, tests.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m compileall -q paurubot migrations
ruff check paurubot tests migrations
ruff format --check paurubot tests migrations
python3 -m mypy paurubot
python3 -m pytest -q "$@"
