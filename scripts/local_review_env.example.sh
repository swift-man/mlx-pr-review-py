#!/bin/zsh
# 이 파일을 local_review_env.sh 로 복사한 뒤 실제 값으로 바꿔서 사용하세요.

CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export HOST=127.0.0.1
export PORT=8000

unset GITHUB_TOKEN
unset GITHUB_APP_PRIVATE_KEY
unset MLX_REVIEW_CMD
unset DRY_RUN

export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY_PATH=/Users/runner/pr-review/mlx-review-bot.2026-03-13.private-key.pem
# 설치 ID를 알고 있으면 사용하고, 모르면 주석 처리한 채 자동 조회에 맡겨도 됩니다.
# export GITHUB_APP_INSTALLATION_ID=12345678

export GITHUB_WEBHOOK_SECRET=replace-me
export GITHUB_REPOSITORY=swift-man/review.gorani.me
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
