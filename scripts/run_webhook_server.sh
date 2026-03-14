#!/bin/zsh
set -euo pipefail

if [[ $# -gt 0 ]]; then
  echo "usage: $0"
  exit 1
fi

ROOT_DIR="${LOCAL_REVIEW_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

: "${GITHUB_WEBHOOK_SECRET:?Set GITHUB_WEBHOOK_SECRET before starting the webhook server}"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  : "${GITHUB_APP_ID:?Set GITHUB_TOKEN or GITHUB_APP_ID before starting the webhook server}"
  if [[ -z "${GITHUB_APP_PRIVATE_KEY:-}" && -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]]; then
    echo "Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH for GitHub App authentication" >&2
    exit 1
  fi
fi

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR"
exec "$ROOT_DIR/venv/bin/uvicorn" review_runner.webhook_app:app --host "$HOST" --port "$PORT"
