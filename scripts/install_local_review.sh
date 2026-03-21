#!/bin/zsh
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "usage: $0 /absolute/target/path"
  exit 1
fi

TARGET_ROOT="$1"
SOURCE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$TARGET_ROOT/review_runner"
mkdir -p "$TARGET_ROOT/scripts"
mkdir -p "$TARGET_ROOT/deploy"
cp "$SOURCE_ROOT/review_runner/"*.py "$TARGET_ROOT/review_runner/"
cp "$SOURCE_ROOT/review_runner/requirements.txt" "$TARGET_ROOT/review_runner/"
cp "$SOURCE_ROOT/scripts/run_webhook_server.sh" "$TARGET_ROOT/scripts/"
cp "$SOURCE_ROOT/scripts/send_test_webhook.sh" "$TARGET_ROOT/scripts/"
cp "$SOURCE_ROOT/scripts/warm_mlx_model.sh" "$TARGET_ROOT/scripts/"
cp "$SOURCE_ROOT/scripts/local_review_env.example.sh" "$TARGET_ROOT/scripts/"
cp "$SOURCE_ROOT/deploy/nginx-pr-review.conf" "$TARGET_ROOT/deploy/"

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
