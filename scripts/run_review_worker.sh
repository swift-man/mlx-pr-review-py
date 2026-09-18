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

# 비워두면 hostname 을 쓴다. 머신마다 다르므로 노드가 1대 1워커면 지정할 필요가 없다.
# 재기동 사이에 이름이 같아야 자기 PEL 을 이어받을 수 있어서 pid 는 넣지 않는다.
# 다만 한 머신에서 워커를 2개 이상 돌릴 때는 반드시 서로 다른 값을 지정해야 한다.
# 기본값이 같으면 두 프로세스가 같은 consumer 이름을 공유해 PEL 이 뒤섞인다.
if [[ -n "${REVIEW_WORKER_NAME:-}" ]]; then
  export REVIEW_WORKER_NAME
fi

# 비밀번호를 지운 형태로만 찍는다. 이 스크립트의 stdout 은 LaunchAgent 가
# /tmp 에 world-readable 로그로 남긴다.
REDIS_URL_SAFE="$(printf '%s' "$REVIEW_REDIS_URL" | sed -E 's|://[^@/]*@|://***@|')"
echo "[worker] redis=$REDIS_URL_SAFE model=${MLX_MODEL:-<default>} backend=${MLX_REVIEW_BACKEND:-local}"

cd "$ROOT_DIR"
exec "$PYTHON_BIN" -m review_runner.review_worker
