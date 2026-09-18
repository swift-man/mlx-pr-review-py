#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$SCRIPT_DIR/local_review_env.sh}"

if [[ -f "$ENV_FILE" ]]; then
  # GitHub App 자격증명과 MLX 설정은 커밋하지 않는 로컬 env 에서 읽는다.
  source "$ENV_FILE"
fi

ROOT_DIR="${LOCAL_REVIEW_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "[worker] python interpreter not found at $PYTHON_BIN" >&2
  exit 127
fi

export PYTHONPATH="$ROOT_DIR"
export REVIEW_REDIS_URL="${REVIEW_REDIS_URL:-redis://127.0.0.1:6379/0}"

# 2대 구성에서는 반드시 서로 다른 이름이어야 한다. 비워두면 hostname-pid 를 쓰므로
# 보통은 지정할 필요가 없다.
if [[ -n "${REVIEW_WORKER_NAME:-}" ]]; then
  export REVIEW_WORKER_NAME
fi

# 비밀번호를 지운 형태로만 찍는다. 이 스크립트의 stdout 은 LaunchAgent 가
# /tmp 에 world-readable 로그로 남긴다.
REDIS_URL_SAFE="$(printf '%s' "$REVIEW_REDIS_URL" | sed -E 's|://[^@/]*@|://***@|')"
echo "[worker] redis=$REDIS_URL_SAFE model=${MLX_MODEL:-<default>} backend=${MLX_REVIEW_BACKEND:-local}"

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m review_runner.review_worker
