#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET_ROOT="${1:-/Users/runner/pr-review}"
ENV_FILE="${LOCAL_REVIEW_ENV_FILE:-$TARGET_ROOT/scripts/local_review_env.sh}"

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

echo "Stopping existing webhook server from $TARGET_ROOT"
pkill -f "$TARGET_ROOT/venv/bin/uvicorn" || true

echo "Installing latest source from $SOURCE_ROOT into $TARGET_ROOT"
PYTHON_BIN="$PYTHON_BIN_RESOLVED" "$SOURCE_ROOT/scripts/install_local_review.sh" "$TARGET_ROOT"

if [[ ! -f "$ENV_FILE" ]]; then
  cat <<EOF
No env file found at:
  $ENV_FILE

Create it first with:
  cp $TARGET_ROOT/scripts/local_review_env.example.sh $TARGET_ROOT/scripts/local_review_env.sh

Then fill in the real GitHub/App credentials and rerun this script.
EOF
  exit 1
fi

echo "Starting webhook server using env file $ENV_FILE"
export LOCAL_REVIEW_HOME="$TARGET_ROOT"
export LOCAL_REVIEW_ENV_FILE="$ENV_FILE"
exec zsh "$TARGET_ROOT/scripts/run_webhook_server.sh"
