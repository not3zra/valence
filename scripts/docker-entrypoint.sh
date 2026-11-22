#!/usr/bin/env bash
set -euo pipefail

echo "==> Applying database migrations"
alembic upgrade head

echo "==> Starting Valence"
exec uvicorn src.main:app --host 0.0.0.0 --port "${PORT:-8080}"
