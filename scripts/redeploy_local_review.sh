#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_ROOT="${1:-/Users/runner/pr-review}"
if [[ "$TARGET_ROOT" != "/" ]]; then
  TARGET_ROOT="${TARGET_ROOT%/}"
fi
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$TARGET_ROOT/scripts/local_review_env.sh}"
LAUNCH_AGENT_LABEL="${LOCAL_REVIEW_LAUNCH_AGENT_LABEL:-com.swiftman.pr-review}"
LAUNCH_AGENT_SERVICE="gui/$(id -u)/$LAUNCH_AGENT_LABEL"

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

  echo "python3.11 or python3 is required" >&2
  exit 1
}

PYTHON_BIN_RESOLVED="$(resolve_python_bin)"

launchagent_is_loaded() {
  command -v launchctl >/dev/null 2>&1 && launchctl print "$LAUNCH_AGENT_SERVICE" >/dev/null 2>&1
}

launchagent_matches_target() {
  local launchagent_details="$1"
  local expected_home_line="LOCAL_REVIEW_HOME => $TARGET_ROOT"

  [[ "$launchagent_details" == *"$TARGET_ROOT/scripts/run_webhook_server.sh"* ]] ||
    [[ "$launchagent_details" == *"$expected_home_line"$'\n'* ]] ||
    [[ "$launchagent_details" == *"$expected_home_line" ]]
}

ensure_launchagent_matches_target() {
  local launchagent_details
  launchagent_details="$(launchctl print "$LAUNCH_AGENT_SERVICE" 2>/dev/null || true)"

  if launchagent_matches_target "$launchagent_details"; then
    return
  fi

  cat <<EOF >&2
LaunchAgent $LAUNCH_AGENT_SERVICE is loaded, but it does not point at this deploy target:
  TARGET_ROOT: $TARGET_ROOT

Expected LaunchAgent to reference one of:
  $TARGET_ROOT/scripts/run_webhook_server.sh
  LOCAL_REVIEW_HOME => $TARGET_ROOT

Refusing to redeploy because kickstart would restart a different installation.
Update ~/Library/LaunchAgents/$LAUNCH_AGENT_LABEL.plist or rerun with the matching target path.
EOF
  exit 1
}

ensure_env_file_exists() {
  if [[ -f "$ENV_FILE" ]]; then
    return
  fi

  cat <<EOF
No env file found at:
  $ENV_FILE

Create it first with:
  cp $TARGET_ROOT/scripts/local_review_env.example.sh $TARGET_ROOT/scripts/local_review_env.sh

Then fill in the real GitHub/App credentials and rerun this script.
EOF
  exit 1
}

print_launchagent_followup() {
  cat <<EOF
LaunchAgent restart requested:
  launchctl kickstart -k $LAUNCH_AGENT_SERVICE

Check server health:
  curl http://127.0.0.1:8000/healthz

Watch logs:
  tail -f /tmp/mlx-pr-review-webhook.log /tmp/mlx-pr-review-webhook.err.log
EOF
}

if launchagent_is_loaded; then
  ensure_launchagent_matches_target
  echo "LaunchAgent $LAUNCH_AGENT_SERVICE is loaded; installing latest source before restart"
  PYTHON_BIN="$PYTHON_BIN_RESOLVED" "$SOURCE_ROOT/scripts/install_local_review.sh" "$TARGET_ROOT"
  ensure_env_file_exists

  echo "Restarting webhook server through LaunchAgent $LAUNCH_AGENT_SERVICE"
  launchctl kickstart -k "$LAUNCH_AGENT_SERVICE"
  print_launchagent_followup
  exit 0
fi

echo "No loaded LaunchAgent found for $LAUNCH_AGENT_SERVICE"
echo "Stopping existing foreground webhook server from $TARGET_ROOT"
pkill -f "$TARGET_ROOT/venv/bin/uvicorn" || true

echo "Installing latest source from $SOURCE_ROOT into $TARGET_ROOT"
PYTHON_BIN="$PYTHON_BIN_RESOLVED" "$SOURCE_ROOT/scripts/install_local_review.sh" "$TARGET_ROOT"
ensure_env_file_exists

echo "Starting webhook server using env file $ENV_FILE"
export LOCAL_REVIEW_HOME="$TARGET_ROOT"
export LOCAL_REVIEW_ENV_FILE="$ENV_FILE"
exec zsh "$TARGET_ROOT/scripts/run_webhook_server.sh"
