#!/bin/zsh
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 /absolute/target/path"
  exit 1
fi

TARGET_ROOT="$1"
SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

copy_file() {
  local source_file="$1"
  local target_file="$2"
  local temp_file="$target_file.tmp.$$"

  cp "$source_file" "$temp_file"
  mv "$temp_file" "$target_file"
}

copy_into_dir() {
  local target_dir="$1"
  shift

  local source_file
  for source_file in "$@"; do
    copy_file "$source_file" "$target_dir/$(basename "$source_file")"
  done
}

mkdir -p "$TARGET_ROOT/review_runner"
mkdir -p "$TARGET_ROOT/scripts"
mkdir -p "$TARGET_ROOT/deploy"
mkdir -p "$TARGET_ROOT/deploy/launchagents"
printf '%s\n' "$SOURCE_ROOT" > "$TARGET_ROOT/.local_review_source_root"
find "$TARGET_ROOT/review_runner" -maxdepth 1 -type f -name '*.py' -delete
copy_into_dir "$TARGET_ROOT/review_runner" "$SOURCE_ROOT/review_runner/"*.py
copy_file "$SOURCE_ROOT/review_runner/requirements.txt" "$TARGET_ROOT/review_runner/requirements.txt"
copy_file "$SOURCE_ROOT/scripts/kickstart_local_review.sh" "$TARGET_ROOT/scripts/kickstart_local_review.sh"
copy_file "$SOURCE_ROOT/scripts/run_webhook_server.sh" "$TARGET_ROOT/scripts/run_webhook_server.sh"
copy_file "$SOURCE_ROOT/scripts/send_test_webhook.sh" "$TARGET_ROOT/scripts/send_test_webhook.sh"
copy_file "$SOURCE_ROOT/scripts/warm_mlx_model.sh" "$TARGET_ROOT/scripts/warm_mlx_model.sh"
copy_file "$SOURCE_ROOT/scripts/local_review_env.example.sh" "$TARGET_ROOT/scripts/local_review_env.example.sh"
copy_file "$SOURCE_ROOT/deploy/nginx-pr-review.conf" "$TARGET_ROOT/deploy/nginx-pr-review.conf"
copy_into_dir "$TARGET_ROOT/deploy/launchagents" "$SOURCE_ROOT/deploy/launchagents/"*.plist

PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys

minimum = (3, 10)
if sys.version_info < minimum:
    version = ".".join(str(part) for part in sys.version_info[:3])
    min_version = ".".join(str(part) for part in minimum)
    raise SystemExit(
        f"Python {min_version}+ is required for MLX installation. Current interpreter: {version}"
    )
PY

if [[ ! -d "$TARGET_ROOT/venv" ]]; then
  "$PYTHON_BIN" -m venv "$TARGET_ROOT/venv"
fi

"$TARGET_ROOT/venv/bin/pip" install --upgrade pip
"$TARGET_ROOT/venv/bin/pip" install -r "$TARGET_ROOT/review_runner/requirements.txt"

cat <<EOF
Installed local review runner into:
  $TARGET_ROOT

Warm the MLX model cache with:
  LOCAL_REVIEW_HOME=$TARGET_ROOT zsh $TARGET_ROOT/scripts/warm_mlx_model.sh

Start the webhook server with:
  LOCAL_REVIEW_HOME=$TARGET_ROOT zsh $TARGET_ROOT/scripts/run_webhook_server.sh

Send a signed webhook test with:
  GITHUB_WEBHOOK_SECRET=... GITHUB_REPOSITORY=OWNER/REPO PULL_NUMBER=123 zsh $TARGET_ROOT/scripts/send_test_webhook.sh
EOF
