#!/bin/zsh
set -euo pipefail

if [[ $# -gt 0 ]]; then
  echo "usage: $0"
  exit 1
fi

ROOT_DIR="${LOCAL_REVIEW_HOME:-$(cd "$(dirname "$0")/.." && pwd)}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# 웹훅 위조 방지를 위해 secret은 항상 필수다.
: "${GITHUB_WEBHOOK_SECRET:?Set GITHUB_WEBHOOK_SECRET before starting the webhook server}"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  # PAT 대신 GitHub App 인증을 쓸 때는 App ID와 private key 둘 다 필요하다.
  : "${GITHUB_APP_ID:?Set GITHUB_TOKEN or GITHUB_APP_ID before starting the webhook server}"
  if [[ -z "${GITHUB_APP_PRIVATE_KEY:-}" && -z "${GITHUB_APP_PRIVATE_KEY_PATH:-}" ]]; then
    echo "Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH for GitHub App authentication" >&2
    exit 1
  fi
fi

cd "$ROOT_DIR"
export PYTHONPATH="$ROOT_DIR"
# uvicorn은 foreground로 실행해 상위 프로세스에서 로그를 그대로 볼 수 있게 둔다.
exec "$ROOT_DIR/venv/bin/uvicorn" review_runner.webhook_app:app --host "$HOST" --port "$PORT"
