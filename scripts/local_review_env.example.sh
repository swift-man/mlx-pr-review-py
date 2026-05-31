#!/bin/zsh
# 이 파일을 local_review_env.sh 로 복사한 뒤 실제 값으로 바꿔서 사용하세요.

CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
# install_local_review.sh 가 기록한 원본 repo 경로를 kickstart 시 자동 사용합니다.
# 원본 경로를 직접 지정하거나 자동 복사를 끄고 싶으면 아래 값을 조정하세요.
# export LOCAL_REVIEW_SOURCE_ROOT=/Users/runner/mlx-pr-review-py
# export LOCAL_REVIEW_SYNC_SOURCE=0
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
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
# Apple Silicon 64GB급 로컬 운영은 품질 우선으로 출력 상한을 넉넉하게 둔다.
export MLX_MAX_TOKENS=1600
# PR 리뷰 입력은 diff만 보지 않고 최신 PR HEAD의 변경 파일 full code를 함께 봅니다.
# webhook 서버(run_webhook_server.sh)는 기본을 full/220000 으로 두어 변경 파일은
# line-numbered 전체 코드로 읽고, diff 는 코멘트 anchor 로만 사용합니다.
# 입력을 줄여야 할 때는 auto(큰 파일 excerpt fallback) 또는 excerpt 로 낮출 수 있습니다.
# 변경 외 repo 파일까지 보려면 full_repo 로 올릴 수 있습니다.
# export MLX_REVIEW_CONTEXT_MODE=full_repo
# export MLX_REVIEW_CONTEXT_MAX_CHARS=220000
# 전체 prompt 가 generate 서버 상한을 넘으면 변경 파일을 여러 묶음으로 나눠 리뷰합니다.
# export MLX_REVIEW_PROMPT_MAX_CHARS=220000
# export MLX_REVIEW_CONTEXT_LINE_RADIUS=120
# export MLX_REVIEW_REPO_CONTEXT_MAX_FILES=120
# export MLX_REVIEW_REPO_CONTEXT_MAX_CHARS=320000
# export MLX_REVIEW_REPO_CONTEXT_FILE_MAX_CHARS=18000
# export MLX_REVIEW_CONTEXT_API_TIMEOUT_SECONDS=20
# Metal/MLX abort가 반복되면 주석을 해제해 CPU fallback으로 확인하세요.
# export MLX_DEVICE=cpu

# GitHub Copilot PR 리뷰 요청은 run_webhook_server.sh 기본값으로 켜져 있습니다.
# 봇은 로컬 budget 파일에 월별 사용량을 기록하고, 같은 PR에는 중복 요청하지 않습니다.
# Free 플랜은 GitHub.com PR code review가 기본 제공되지 않을 수 있으므로 실패하면 MLX 리뷰만 계속합니다.
# 필요하면 export 없이 값만 지정해 시작 스크립트 기본값을 덮어씁니다.
# COPILOT_REVIEW_REQUEST=0
# COPILOT_REVIEW_MONTHLY_BUDGET=50
# COPILOT_REVIEW_REQUEST_COST=13

# ───────────────────────────────────────────────────────────────────────────
# 원격 MLX backend (mlx-final-py 의 /v1/generate 와 모델 인스턴스 공유)
# ───────────────────────────────────────────────────────────────────────────
# 같은 호스트에서 mlx-final-py (port 8002, ~17GB Qwen3-30B-A3B) 가 이미 모델을
# 메모리에 들고 있는 환경이면 webhook 프로세스가 별도로 모델을 또 로드하지 않고
# HTTP 로 위임하도록 한다. 두 프로세스가 한 모델을 공유 → 메모리 17GB 절약.
#
# 비활성화 (in-process 로컬 backend) 가 기본값. 켜려면 아래 두 줄을 주석 해제.
# export MLX_REVIEW_BACKEND=remote
# export MLX_GENERATE_URL=http://127.0.0.1:8002/v1/generate
# Bearer 인증을 사용한다면 mlx-final-py 와 같은 토큰을 export.
# export MLX_GENERATE_AUTH_TOKEN=replace-me
# 응답 timeout (초). 초과하면 같은 장기 생성 요청을 다시 보내지 않고 timeout 으로 실패 처리한다.
export MLX_GENERATE_TIMEOUT=900
# 요청 body 상한 (bytes). mlx-final-py 의 MLX_HTTP_BODY_MAX_BYTES 와 같은 값으로 맞추세요.
export MLX_GENERATE_CLIENT_MAX_BODY_BYTES=4194304

export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
