#!/usr/bin/env bash
set -euo pipefail
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-18083}"
exec uvicorn app:app --host "$HOST" --port "$PORT"
