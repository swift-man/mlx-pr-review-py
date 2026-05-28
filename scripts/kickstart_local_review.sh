#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_REVIEW_HOME="${LOCAL_REVIEW_HOME:-$DEFAULT_HOME}"
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$LOCAL_REVIEW_HOME/scripts/local_review_env.sh}"
SOURCE_ROOT_FILE="$LOCAL_REVIEW_HOME/.local_review_source_root"

if [[ -f "$ENV_FILE" ]]; then
  set +u
  source "$ENV_FILE"
  set -u
fi

LAUNCH_AGENT_LABEL="${LOCAL_REVIEW_LAUNCH_AGENT_LABEL:-com.swiftman.pr-review}"
LAUNCH_AGENT_SERVICE="gui/$(id -u)/$LAUNCH_AGENT_LABEL"
PORT="${PORT:-8000}"
HEALTH_HOST="${LOCAL_REVIEW_HEALTH_HOST:-127.0.0.1}"
HEALTH_URL="${LOCAL_REVIEW_HEALTH_URL:-http://$HEALTH_HOST:$PORT/healthz}"
HEALTH_TIMEOUT="${LOCAL_REVIEW_HEALTH_TIMEOUT:-2}"
STDOUT_LOG="${LOCAL_REVIEW_WEBHOOK_LOG:-/tmp/mlx-pr-review-webhook.log}"
STDERR_LOG="${LOCAL_REVIEW_WEBHOOK_ERR_LOG:-/tmp/mlx-pr-review-webhook.err.log}"
TAIL_LINES="${LOCAL_REVIEW_TAIL_LINES:-80}"
TAIL_LOGS="${LOCAL_REVIEW_TAIL_LOGS:-1}"
SYNC_SOURCE="${LOCAL_REVIEW_SYNC_SOURCE:-1}"

resolve_python_bin() {
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    echo "$PYTHON_BIN"
    return
  fi

  if command -v python3.11 >/dev/null 2>&1; then
    command -v python3.11
    return
  fi

  if command -v brew >/dev/null 2>&1; then
    local brew_py311
    brew_py311="$(brew --prefix python@3.11 2>/dev/null || true)"
    if [[ -n "$brew_py311" && -x "$brew_py311/bin/python3.11" ]]; then
      echo "$brew_py311/bin/python3.11"
      return
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return
  fi

  echo "python3.11 or python3 is required to sync latest source" >&2
  exit 1
}

resolve_source_root() {
  local source_root="${LOCAL_REVIEW_SOURCE_ROOT:-}"

  if [[ -z "$source_root" && -f "$SOURCE_ROOT_FILE" ]]; then
    IFS= read -r source_root < "$SOURCE_ROOT_FILE" || true
  fi

  if [[ -z "$source_root" ]]; then
    return
  fi

  if [[ ! -d "$source_root" ]]; then
    echo "Configured source root does not exist: $source_root" >&2
    echo "Set LOCAL_REVIEW_SOURCE_ROOT or rerun redeploy_local_review.sh from the source repo." >&2
    exit 1
  fi

  (cd "$source_root" && pwd)
}

sync_latest_source() {
  if [[ "$SYNC_SOURCE" == "0" ]]; then
    echo "Skipping source sync because LOCAL_REVIEW_SYNC_SOURCE=0"
    return
  fi

  local source_root
  source_root="$(resolve_source_root)"
  if [[ -z "$source_root" ]]; then
    echo "No source root metadata found; restarting without source sync"
    return
  fi

  if [[ "$source_root" == "$LOCAL_REVIEW_HOME" ]]; then
    echo "Source root and deploy root are the same; skipping source sync"
    return
  fi

  if [[ ! -f "$source_root/scripts/install_local_review.sh" ]]; then
    echo "Source root is missing scripts/install_local_review.sh: $source_root" >&2
    exit 1
  fi

  echo "Syncing latest source before restart:"
  echo "  source: $source_root"
  echo "  target: $LOCAL_REVIEW_HOME"
  PYTHON_BIN="$(resolve_python_bin)" "$source_root/scripts/install_local_review.sh" "$LOCAL_REVIEW_HOME"
}

if ! command -v launchctl >/dev/null 2>&1; then
  echo "launchctl is required to restart $LAUNCH_AGENT_SERVICE" >&2
  exit 1
fi

sync_latest_source

echo "Restarting webhook server through LaunchAgent $LAUNCH_AGENT_SERVICE"
launchctl kickstart -k "$LAUNCH_AGENT_SERVICE"

echo "Checking server health:"
echo "  $HEALTH_URL"

health_ok=0
for _ in {1..20}; do
  if response="$(curl -fsS --max-time "$HEALTH_TIMEOUT" "$HEALTH_URL" 2>/dev/null)"; then
    echo "$response"
    health_ok=1
    break
  fi
  sleep 0.5
done

if [[ "$health_ok" != "1" ]]; then
  echo "Health check failed after restart. Check logs:" >&2
  echo "  tail -f $STDOUT_LOG $STDERR_LOG" >&2
  exit 1
fi

if [[ "$TAIL_LOGS" == "0" ]]; then
  echo "Skipping log tail because LOCAL_REVIEW_TAIL_LOGS=0"
  exit 0
fi

echo "Watching logs. Press Ctrl-C to stop watching; the LaunchAgent keeps running."
echo "  tail -n $TAIL_LINES -F $STDOUT_LOG $STDERR_LOG"
exec tail -n "$TAIL_LINES" -F "$STDOUT_LOG" "$STDERR_LOG"
