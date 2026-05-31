#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REQUESTED_LOCAL_REVIEW_HOME="${LOCAL_REVIEW_HOME:-}"
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$SCRIPT_DIR/local_review_env.sh}"

if [[ -f "$ENV_FILE" ]]; then
  # 운영용 값은 로컬 전용 env 스크립트에서 읽고, 저장소에는 커밋하지 않는다.
  source "$ENV_FILE"
fi

if [[ -n "$REQUESTED_LOCAL_REVIEW_HOME" ]]; then
  LOCAL_REVIEW_HOME="$REQUESTED_LOCAL_REVIEW_HOME"
fi

if [[ $# -gt 0 ]]; then
  echo "usage: $0"
  exit 1
fi

ROOT_DIR="${LOCAL_REVIEW_HOME:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# Copilot PR review 요청은 서버 시작 스크립트 기본값으로 관리한다.
# local_review_env.sh 에서 같은 이름의 값을 지정하면 이 기본값을 덮어쓸 수 있다.
COPILOT_REVIEW_REQUEST="${COPILOT_REVIEW_REQUEST:-1}"
COPILOT_REVIEW_MONTHLY_BUDGET="${COPILOT_REVIEW_MONTHLY_BUDGET:-50}"
COPILOT_REVIEW_REQUEST_COST="${COPILOT_REVIEW_REQUEST_COST:-13}"
COPILOT_REVIEWER="${COPILOT_REVIEWER:-copilot}"

# 리뷰 입력 컨텍스트 기본값도 시작 스크립트에서 관리한다.
# 코드 기본값(review_service.DEFAULT_CURRENT_FILE_CONTEXT_MODE)도 auto 라 CLI 와
# webhook 이 같은 동작을 하며, 여기서는 운영 기본값을 한곳에서 명시해 둔다.
# auto 는 변경 파일만 컨텍스트로 주되, MAX_CHARS 를 넘는 큰 파일은 변경 hunk 주변
# excerpt 로 보존해 full(초과 시 앞부분만 남기고 잘림)보다 안전하게 입력을 줄인다.
# 변경 외 repo 파일까지 붙이는 full_repo 는 입력이 수십만 자로 커져 remote
# /v1/generate prefill 이 길어지므로, 품질 우선이 필요할 때만 명시해 올린다.
# local_review_env.sh 에서 같은 이름을 지정하면 이 기본값을 덮어쓸 수 있다.
MLX_REVIEW_CONTEXT_MODE="${MLX_REVIEW_CONTEXT_MODE:-auto}"
MLX_REVIEW_CONTEXT_MAX_CHARS="${MLX_REVIEW_CONTEXT_MAX_CHARS:-18000}"

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
# uvicorn은 foreground로 실행해 상위 프로세스에서 로그를 그대로 볼 수 있게 둔다.
exec env \
  PYTHONPATH="$ROOT_DIR" \
  COPILOT_REVIEW_REQUEST="$COPILOT_REVIEW_REQUEST" \
  COPILOT_REVIEW_MONTHLY_BUDGET="$COPILOT_REVIEW_MONTHLY_BUDGET" \
  COPILOT_REVIEW_REQUEST_COST="$COPILOT_REVIEW_REQUEST_COST" \
  COPILOT_REVIEWER="$COPILOT_REVIEWER" \
  MLX_REVIEW_CONTEXT_MODE="$MLX_REVIEW_CONTEXT_MODE" \
  MLX_REVIEW_CONTEXT_MAX_CHARS="$MLX_REVIEW_CONTEXT_MAX_CHARS" \
  "$ROOT_DIR/venv/bin/uvicorn" review_runner.webhook_app:app --host "$HOST" --port "$PORT"
