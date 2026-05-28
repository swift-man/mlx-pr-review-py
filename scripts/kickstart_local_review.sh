#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DEFAULT_HOME="$(cd "$SCRIPT_DIR/.." && pwd)"
LOCAL_REVIEW_HOME="${LOCAL_REVIEW_HOME:-$DEFAULT_HOME}"
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$LOCAL_REVIEW_HOME/scripts/local_review_env.sh}"

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
STDOUT_LOG="${LOCAL_REVIEW_WEBHOOK_LOG:-/tmp/mlx-pr-review-webhook.log}"
STDERR_LOG="${LOCAL_REVIEW_WEBHOOK_ERR_LOG:-/tmp/mlx-pr-review-webhook.err.log}"
TAIL_LINES="${LOCAL_REVIEW_TAIL_LINES:-80}"
TAIL_LOGS="${LOCAL_REVIEW_TAIL_LOGS:-1}"

if ! command -v launchctl >/dev/null 2>&1; then
  echo "launchctl is required to restart $LAUNCH_AGENT_SERVICE" >&2
  exit 1
fi

echo "Restarting webhook server through LaunchAgent $LAUNCH_AGENT_SERVICE"
launchctl kickstart -k "$LAUNCH_AGENT_SERVICE"

echo "Checking server health:"
echo "  $HEALTH_URL"

health_ok=0
for _ in {1..20}; do
  if response="$(curl -fsS "$HEALTH_URL" 2>/dev/null)"; then
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
