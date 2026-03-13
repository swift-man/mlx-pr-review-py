#!/bin/zsh
set -euo pipefail

if [[ $# -gt 0 ]]; then
  echo "usage: $0"
  echo
  echo "required env for pull_request:"
  echo "  GITHUB_WEBHOOK_SECRET"
  echo "  GITHUB_REPOSITORY"
  echo "  PULL_NUMBER"
  echo
  echo "optional env:"
  echo "  WEBHOOK_URL   (default: http://127.0.0.1:\${PORT:-8000}/github/webhook)"
  echo "  WEBHOOK_EVENT (default: pull_request, also supports ping)"
  echo "  PR_ACTION     (default: opened)"
  echo "  PR_DRAFT      (default: false)"
  echo "  DELIVERY_ID   (default: manual-<timestamp>)"
  echo "  PAYLOAD_FILE  (send an exact JSON payload from file instead of generating one)"
  exit 1
fi

: "${GITHUB_WEBHOOK_SECRET:?Set GITHUB_WEBHOOK_SECRET before sending a webhook}"

WEBHOOK_URL="${WEBHOOK_URL:-http://127.0.0.1:${PORT:-8000}/github/webhook}"
WEBHOOK_EVENT="${WEBHOOK_EVENT:-pull_request}"
PR_ACTION="${PR_ACTION:-opened}"
PR_DRAFT="${PR_DRAFT:-false}"
DELIVERY_ID="${DELIVERY_ID:-manual-$(date +%s)}"

payload_path="${PAYLOAD_FILE:-}"
cleanup_payload=0

if [[ -z "$payload_path" ]]; then
  payload_path="$(mktemp)"
  cleanup_payload=1

  if [[ "$WEBHOOK_EVENT" == "ping" ]]; then
    cat >"$payload_path" <<'EOF'
{"zen":"Keep it logically awesome.","hook_id":1}
EOF
  else
    : "${GITHUB_REPOSITORY:?Set GITHUB_REPOSITORY for pull_request webhook tests}"
    : "${PULL_NUMBER:?Set PULL_NUMBER for pull_request webhook tests}"
    PAYLOAD_REPOSITORY="$GITHUB_REPOSITORY" \
    PAYLOAD_PULL_NUMBER="$PULL_NUMBER" \
    PAYLOAD_ACTION="$PR_ACTION" \
    PAYLOAD_DRAFT="$PR_DRAFT" \
    python3 - <<'PY' >"$payload_path"
import json
import os
import sys

draft = os.environ["PAYLOAD_DRAFT"].strip().lower() in {"1", "true", "yes", "on"}
pull_number = int(os.environ["PAYLOAD_PULL_NUMBER"])
payload = {
    "action": os.environ["PAYLOAD_ACTION"],
    "number": pull_number,
    "pull_request": {
        "number": pull_number,
        "draft": draft,
    },
    "repository": {
        "full_name": os.environ["PAYLOAD_REPOSITORY"],
    },
}
json.dump(payload, sys.stdout, ensure_ascii=False, separators=(",", ":"))
PY
  fi
fi

cleanup() {
  if [[ "$cleanup_payload" -eq 1 && -f "$payload_path" ]]; then
    rm -f "$payload_path"
  fi
}
trap cleanup EXIT

signature="$(
  PAYLOAD_PATH="$payload_path" \
  WEBHOOK_SECRET="$GITHUB_WEBHOOK_SECRET" \
  python3 - <<'PY'
import hashlib
import hmac
import os

with open(os.environ["PAYLOAD_PATH"], "rb") as fh:
    payload = fh.read()

secret = os.environ["WEBHOOK_SECRET"].encode("utf-8")
print("sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest())
PY
)"

echo "POST $WEBHOOK_URL"
echo "event=$WEBHOOK_EVENT delivery=$DELIVERY_ID"

curl --silent --show-error \
  --write-out $'\nHTTP %{http_code}\n' \
  --request POST \
  --header "Content-Type: application/json" \
  --header "X-GitHub-Event: $WEBHOOK_EVENT" \
  --header "X-GitHub-Delivery: $DELIVERY_ID" \
  --header "X-Hub-Signature-256: $signature" \
  --data-binary "@$payload_path" \
  "$WEBHOOK_URL"
