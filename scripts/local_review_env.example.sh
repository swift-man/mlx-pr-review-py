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
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
# Metal/MLX abort가 반복되면 주석을 해제해 CPU fallback으로 확인하세요.
# export MLX_DEVICE=cpu

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
# 응답 timeout (초). 초과하면 같은 장기 생성 요청을 다시 보내지 않고 timeout 으로 실패 처리한다 (default 600).
# export MLX_GENERATE_TIMEOUT=600

export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
