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

resolve_server_port() {
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "${PORT:-8000}"
    return
  fi

  (
    set +e
    set +u
    source "$ENV_FILE" >/dev/null 2>&1
    echo "${PORT:-8000}"
  )
}

review_server_command_matches() {
  local command_line="$1"

  [[ "$command_line" == *"$TARGET_ROOT/venv/bin/uvicorn"* ]] ||
    [[ "$command_line" == *"$TARGET_ROOT/scripts/run_webhook_server.sh"* ]] ||
    ([[ "$command_line" == *"$TARGET_ROOT"* ]] && [[ "$command_line" == *"review_runner.webhook_app:app"* ]])
}

stop_process() {
  local pid="$1"
  local command_line="$2"

  echo "Stopping existing webhook server process on target port: pid=$pid"
  echo "  $command_line"
  kill "$pid" 2>/dev/null || return

  for _ in {1..10}; do
    if ! kill -0 "$pid" 2>/dev/null; then
      return
    fi
    sleep 0.2
  done

  echo "Process $pid did not exit after TERM; sending KILL"
  kill -KILL "$pid" 2>/dev/null || true
}

stop_target_port_listener() {
  local port="$1"
  local pid
  local command_line
  local pids

  if ! command -v lsof >/dev/null 2>&1; then
    echo "lsof not found; skipping target port listener check"
    return
  fi

  pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -z "$pids" ]]; then
    return
  fi

  for pid in ${(f)pids}; do
    command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    if [[ -z "$command_line" ]]; then
      continue
    fi

    if review_server_command_matches "$command_line"; then
      stop_process "$pid" "$command_line"
      continue
    fi

    cat <<EOF >&2
Port $port is already in use by a process that does not look like this review server:
  pid: $pid
  command: $command_line

Refusing to stop an unrelated process. Stop it manually or change PORT in:
  $ENV_FILE
EOF
    exit 1
  done
}

print_launchagent_followup() {
  cat <<EOF
LaunchAgent restart completed and health check passed.

Watch logs with:
  zsh $TARGET_ROOT/scripts/kickstart_local_review.sh
EOF
}

if launchagent_is_loaded; then
  ensure_launchagent_matches_target
  echo "LaunchAgent $LAUNCH_AGENT_SERVICE is loaded; installing latest source before restart"
  PYTHON_BIN="$PYTHON_BIN_RESOLVED" "$SOURCE_ROOT/scripts/install_local_review.sh" "$TARGET_ROOT"
  ensure_env_file_exists

  LOCAL_REVIEW_HOME="$TARGET_ROOT" \
    LOCAL_REVIEW_ENV_FILE="$ENV_FILE" \
    LOCAL_REVIEW_TAIL_LOGS=0 \
    zsh "$TARGET_ROOT/scripts/kickstart_local_review.sh"
  print_launchagent_followup
  exit 0
fi

echo "No loaded LaunchAgent found for $LAUNCH_AGENT_SERVICE"
echo "Stopping existing foreground webhook server from $TARGET_ROOT"
pkill -f "$TARGET_ROOT/venv/bin/uvicorn" || true
stop_target_port_listener "$(resolve_server_port)"

echo "Installing latest source from $SOURCE_ROOT into $TARGET_ROOT"
PYTHON_BIN="$PYTHON_BIN_RESOLVED" "$SOURCE_ROOT/scripts/install_local_review.sh" "$TARGET_ROOT"
ensure_env_file_exists
stop_target_port_listener "$(resolve_server_port)"

echo "Starting webhook server using env file $ENV_FILE"
export LOCAL_REVIEW_HOME="$TARGET_ROOT"
export LOCAL_REVIEW_ENV_FILE="$ENV_FILE"
exec zsh "$TARGET_ROOT/scripts/run_webhook_server.sh"
