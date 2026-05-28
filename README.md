# Mac mini MLX PR Review Webhook

이 저장소는 GitHub webhook을 받아 PR diff를 읽고, MLX로 리뷰한 뒤 GitHub Review API로
라인 코멘트와 전체 리뷰를 등록하는 서버 구성을 담고 있습니다.

## 목표 구조

Mac mini 안의 고정 경로 예시:

```text
/Users/runner/pr-review/
├── deploy/
│   └── nginx-pr-review.conf
├── review_runner/
│   ├── __init__.py
│   ├── mlx_review_client.py
│   ├── mock_review_client.py
│   ├── review_service.py
│   ├── review_pr.py
│   ├── sample_mlx_client.py
│   ├── webhook_app.py
│   └── requirements.txt
├── scripts/
│   ├── warm_mlx_model.sh
│   ├── send_test_webhook.sh
│   └── run_webhook_server.sh
└── venv/
```

외부 트래픽은 Nginx가 받고, FastAPI 서버는 로컬에서 `/github/webhook`만 처리합니다.

## 1. 전제 조건

Mac mini에서 아래 항목이 준비되어 있어야 합니다.

- Homebrew
- Python 3.11 + `venv`
- Nginx
- GitHub webhook secret
- GitHub Review API를 호출할 인증 정보
- MLX 실행 커맨드

Python 3.11이 아직 없다면 먼저 설치합니다.

```bash
brew install python@3.11
"$(brew --prefix python@3.11)/bin/python3.11" --version
```

## 2. 설치와 코드 동기화

이 저장소를 checkout한 위치에서 아래 명령으로 러너 디렉터리 `/Users/runner/pr-review`를 맞춥니다.
처음 설치할 때도, 코드가 바뀐 뒤 업데이트할 때도 같은 명령을 다시 실행하면 됩니다.

```bash
PY311="$(brew --prefix python@3.11)/bin/python3.11"
PYTHON_BIN="$PY311" ./scripts/install_local_review.sh /Users/runner/pr-review
```

이 스크립트는 `review_runner/requirements.txt`를 설치하면서 `mlx-lm`과 `certifi`도 같이 설치합니다.
`pip` 명령이 PATH에 없어도 괜찮고, 이후에는 항상 venv 안의 Python으로 실행하면 됩니다.

설치 직후 아래 두 줄이 정상이어야 합니다.

```bash
/Users/runner/pr-review/venv/bin/python --version
/Users/runner/pr-review/venv/bin/python -c 'import mlx_lm, certifi; print("deps ok")'
```

## 3. 운영에 필요한 환경 변수

기본으로 사용하는 값은 아래와 같습니다.

- `LOCAL_REVIEW_HOME=/Users/runner/pr-review`
- `HOST=127.0.0.1`
- `PORT=8000`
- `GITHUB_TOKEN=...` 또는 아래 GitHub App 설정
- `GITHUB_APP_ID=123456` (옵션)
- `GITHUB_APP_PRIVATE_KEY_PATH=/absolute/path/to/github-app.pem` (옵션)
- `GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."` (옵션)
- `GITHUB_APP_INSTALLATION_ID=12345678` (옵션, 생략 시 `OWNER/REPO` 기준 자동 조회)
- `GITHUB_WEBHOOK_SECRET=...`
- `MLX_REVIEW_CMD=/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client` (옵션, local backend용)
- `MLX_REVIEW_BACKEND=local` 또는 `remote` (옵션, 기본값은 `local`)
- `MLX_GENERATE_URL=http://127.0.0.1:8002/v1/generate` (remote backend용)
- `MLX_GENERATE_AUTH_TOKEN=...` (옵션, remote generate 서버가 Bearer 인증을 쓸 때)
- `MLX_GENERATE_TIMEOUT=360` (옵션, remote generate 응답 timeout 초. 초과 시 같은 장기 생성 요청을 재시도하지 않고 명확한 timeout 오류를 남김)
- `MLX_GENERATE_CLIENT_MAX_BODY_BYTES=4194304` (옵션, remote generate 요청 body 상한. 서버의 `MLX_HTTP_BODY_MAX_BYTES`와 맞춰 설정)
- `MLX_MODEL=mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit` (운영 예시값. local 클라이언트 코드 기본값은 7B)
- `MLX_DEVICE=cpu` (옵션, Metal 장애 시 fallback. 비워두면 MLX 기본 장치 사용)
- `GITHUB_API_URL=https://api.github.com` (옵션)
- `MLX_MAX_TOKENS=1600` (옵션, 모델 출력 토큰 상한. Apple Silicon 64GB급 로컬 운영은 품질 우선으로 넉넉하게 둠)
- `MLX_MAX_FINDINGS=10` (옵션)
- `MLX_REVIEW_CONTEXT_MODE=full_repo` (옵션, 기본값. 변경 파일 context에 더해 예산 안의 repo 파일을 `repository_context`로 추가. `auto`, `full`, `excerpt`, `off` 가능)
- `MLX_REVIEW_CONTEXT_MAX_CHARS=30000` (옵션, 파일별 current code context 최대 길이)
- `MLX_REVIEW_CONTEXT_LINE_RADIUS=120` (옵션, 큰 파일 excerpt에서 hunk 앞뒤로 포함할 기본 줄 수)
- `MLX_REVIEW_REPO_CONTEXT_MAX_FILES=120` (옵션, `full_repo` 모드에서 추가로 읽을 변경 외 repo 파일 수 상한)
- `MLX_REVIEW_REPO_CONTEXT_MAX_CHARS=320000` (옵션, `repository_context` 전체 문자 상한)
- `MLX_REVIEW_REPO_CONTEXT_FILE_MAX_CHARS=18000` (옵션, `repository_context` 파일별 문자 상한)
- `MLX_TRUST_REMOTE_CODE=0` (옵션)
- `COPILOT_REVIEW_BUDGET_FILE=/absolute/path/to/copilot-budget.json` (옵션, Copilot 요청 budget 파일 경로 직접 지정)
- `COPILOT_REVIEW_API_TIMEOUT_SECONDS=10` (옵션, Copilot reviewer 조회/요청 GitHub API timeout)
- `COPILOT_REVIEW_PENDING_TTL_SECONDS=600` (옵션, 결과가 모호한 pending 요청을 재처리하기 전 대기 시간)
- `DRY_RUN=1` (옵션, 실제 GitHub 리뷰를 남기지 않고 흐름만 확인할 때)

`GITHUB_WEBHOOK_SECRET`는 GitHub 저장소 Webhook 설정의 Secret과 반드시 같은 값이어야 합니다.
`GITHUB_REPOSITORY`는 수동 테스트 때 `swift-man/review.gorani.me`처럼 `OWNER/REPO` 형식이어야 합니다.
GitHub App 인증을 쓰고 싶다면 `GITHUB_APP_ID`와 private key를 설정하면 되고, 이 경우 `GITHUB_TOKEN`보다 GitHub App installation token이 우선 사용됩니다.
GitHub App으로 인증하면 리뷰 작성자가 개인 계정이 아니라 App bot으로 보입니다.
`MLX_REVIEW_BACKEND`를 비워 두면 `local`로 동작합니다. 이때 `MLX_REVIEW_CMD`를 비워 두거나
기본값인 `python -m review_runner.mlx_review_client`를 쓰면 웹훅 서버 프로세스 안에서 MLX 모델을
재사용하고, 동시 리뷰 요청은 메모리 급증을 막기 위해 한 번에 하나씩 처리합니다.
`MLX_REVIEW_BACKEND=remote`로 설정하거나 `MLX_GENERATE_URL`만 설정하면 `mlx-final-py`의
`POST /v1/generate`로 chat messages를 보내고, 웹훅 서버는 `mlx-lm`을 import하지 않습니다.
remote backend 요청 body가 `MLX_GENERATE_CLIENT_MAX_BODY_BYTES`를 넘으면 큰 diff를 전송하다
`Broken pipe`로 실패하기 전에 명확한 오류를 남깁니다. 큰 PR은 대상 저장소의 `.reviewbot.yml`
또는 내장 generated-file 필터로 빌드 산출물과 생성 문서를 제외하는 쪽을 우선합니다.
실제 GitHub Review API 연동만 검증할 때는 `review_runner.mock_review_client` 같은 커스텀 커맨드로 바꿔서 테스트할 수 있습니다.

Copilot과 함께 리뷰하려면 먼저 GitHub PR에서 Copilot 리뷰어를 요청한 뒤 이 봇을 실행하면 됩니다.
예를 들어 Copilot PR 리뷰 권한이 있는 계정에서는 아래처럼 수동으로 붙일 수 있습니다.

```bash
gh pr edit 123 --add-reviewer @copilot
```

자동 Copilot 리뷰 요청은 `scripts/run_webhook_server.sh` 기본값으로 켜져 있습니다.
기본값은 아래와 같습니다.

```bash
COPILOT_REVIEW_REQUEST=1
COPILOT_REVIEW_MONTHLY_BUDGET=50
COPILOT_REVIEW_REQUEST_COST=13
```

이 상태에서 봇은 PR마다 한 번만 Copilot을 reviewer로 요청하고, 로컬 budget 파일에 월별 사용량을
기록합니다. budget 파일 경로는 `COPILOT_REVIEW_BUDGET_FILE`, `$LOCAL_REVIEW_HOME/.copilot_review_budget.json`,
`~/.mlx-pr-review-copilot-budget.json` 순서로 결정됩니다. GitHub가 현재 계정,
조직 정책, Copilot 플랜 또는 권한 때문에 Copilot 리뷰 요청을 거절하면 MLX 리뷰는 계속 진행하고
로그에 실패 이유만 남깁니다. 요청 POST 이후 timeout처럼 GitHub 처리 여부가 모호한 경우에는 requested
reviewer를 다시 조회합니다. timeout 기본값은 10초이며 `COPILOT_REVIEW_API_TIMEOUT_SECONDS`로 조정할 수 있습니다.
확인도 실패하면 budget 기록을 pending으로 유지해 이후 TTL 재처리에서 보수적으로 판단합니다.
pending TTL 기본값은 10분이며 `COPILOT_REVIEW_PENDING_TTL_SECONDS`로 조정할 수 있습니다.
GitHub 문서상 Copilot Free는 GitHub.com PR code review가 기본 제공되는
플랜이 아니므로, Free 계정에서는 조직 정책이나 권한 설정에 따라 요청이 거절될 수 있습니다.
임시로 끄거나 예산을 조정해야 할 때만 `local_review_env.sh`에 `export` 없이 값만 지정하면 됩니다.

이 봇은 리뷰 생성 전에 PR의 review comments, issue comments, review thread 대댓글을 읽어
프롬프트에 함께 넣습니다. Copilot이나 다른 봇이 이미 지적했거나 작성자가 반박한 내용은 최신 PR
HEAD 기준으로 다시 증명될 때만 MLX 라인 코멘트로 남기도록 유도해, 같은 false positive를 반복하는
일을 줄입니다. 리뷰 본문은 `MLX 리뷰`와 `Copilot 리뷰` 섹션으로 나뉘며, Copilot이 직접 단
라인 코멘트는 다시 복사하지 않고 Copilot 섹션에 상태와 요약만 표시합니다. Copilot 코멘트가 없으면
리뷰 본문에 빈 Copilot 섹션을 만들지 않습니다. 댓글 조회 권한이 부족하면 해당 context는 건너뛰고
로그에 이유를 남긴 뒤 diff 리뷰는 계속 진행합니다.

매번 `export`를 다시 입력하기 귀찮다면 `/Users/runner/pr-review/scripts/local_review_env.example.sh`를
`/Users/runner/pr-review/scripts/local_review_env.sh`로 복사해 실제 값만 넣어두면 됩니다.
이 파일은 [`scripts/run_webhook_server.sh`](/Users/m4_25/develop/codereview/scripts/run_webhook_server.sh)와
[`scripts/send_test_webhook.sh`](/Users/m4_25/develop/codereview/scripts/send_test_webhook.sh)가 자동으로 읽습니다.

```bash
cp /Users/runner/pr-review/scripts/local_review_env.example.sh /Users/runner/pr-review/scripts/local_review_env.sh
```

처음 요청에서 모델을 다운받게 하지 않으려면 미리 warm-up을 한 번 실행해두는 편이 좋습니다.
이 스크립트는 모델 다운로드와 로컬 캐시를 미리 채우는 용도이고, 상주 메모리 로드는 웹훅 서버 프로세스에서 첫 실요청 때 이뤄집니다.

```bash
export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export MLX_MODEL=mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit
zsh /Users/runner/pr-review/scripts/warm_mlx_model.sh
```

GitHub App bot으로 리뷰를 남기고 싶다면 warm-up과 별개로 아래 환경 변수도 준비해두면 됩니다.

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY_PATH=/Users/runner/pr-review/github-app.private-key.pem
# 선택: 설치 ID를 알고 있으면 직접 지정
export GITHUB_APP_INSTALLATION_ID=12345678
```

## 4. 서버 시작

```bash
CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export HOST=127.0.0.1
export PORT=8000
export GITHUB_TOKEN=ghp_xxx
export GITHUB_WEBHOOK_SECRET=replace-me
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
# export MLX_DEVICE=cpu
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
export DRY_RUN=1
zsh /Users/runner/pr-review/scripts/run_webhook_server.sh
```

GitHub App bot으로 실행할 때는 `GITHUB_TOKEN` 대신 아래처럼 App 환경 변수를 사용하면 됩니다.

```bash
CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export HOST=127.0.0.1
export PORT=8000
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY_PATH=/Users/runner/pr-review/github-app.private-key.pem
export GITHUB_APP_INSTALLATION_ID=12345678
export GITHUB_WEBHOOK_SECRET=replace-me
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
# export MLX_DEVICE=cpu
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
export DRY_RUN=1
zsh /Users/runner/pr-review/scripts/run_webhook_server.sh
```

FastAPI 앱 엔트리포인트는 [`review_runner/webhook_app.py`](/Users/m4_25/develop/codereview/review_runner/webhook_app.py)입니다.
기본 바인딩 주소는 `127.0.0.1:8000`이며, 실제 코드는 [`scripts/run_webhook_server.sh`](/Users/m4_25/develop/codereview/scripts/run_webhook_server.sh)에서 `HOST`와 `PORT`를 읽습니다.

정상 기동 확인:

```bash
curl http://127.0.0.1:8000/healthz
```

응답 예시:

```json
{"status":"ok"}
```

실제 리뷰를 GitHub에 남기려면 `DRY_RUN`을 export 하지 않거나 `unset DRY_RUN` 한 뒤 다시 서버를 띄웁니다.

## 5. 서버 종료

포그라운드에서 실행 중인 터미널이면 `Ctrl-C`로 종료해도 됩니다.
백그라운드나 다른 셸에서 종료하려면 아래 명령을 사용합니다.

```bash
pkill -f '/Users/runner/pr-review/venv/bin/uvicorn' || true
```

## 6. 서버 재시작

LaunchAgent 기준 운영 환경에서는 코드 변경 반영과 재시동을 아래 한 명령으로 처리합니다.

```bash
zsh /Users/runner/pr-review/scripts/kickstart_local_review.sh
```

`install_local_review.sh`가 배포 복사본에 원본 repo 경로를 기록해 두므로, 이 스크립트는
재시동 전에 최신 소스를 `/Users/runner/pr-review`로 먼저 복사합니다. 그 다음
`launchctl kickstart -k`, `/healthz` 확인, 로그 tail을 순서대로 실행합니다. 자동화에서
로그 tail 없이 종료해야 하면 `LOCAL_REVIEW_TAIL_LOGS=0`을 함께 넘깁니다. 이미 복사된
서버만 재시동하고 싶으면 `LOCAL_REVIEW_SYNC_SOURCE=0`을 함께 넘깁니다.

재시동 없이 로그만 보려면 아래 명령을 사용합니다.

```bash
tail -f /tmp/mlx-pr-review-webhook.log /tmp/mlx-pr-review-webhook.err.log
```

처음 설치하거나 원본 repo/배포 대상 경로를 바꿨다면 원본 repo에서 아래 명령으로 배포
복사본과 source metadata를 다시 잡아줍니다.

```bash
./scripts/redeploy_local_review.sh /Users/runner/pr-review
```

LaunchAgent가 등록되어 있지 않은 개발 환경에서는 기존 uvicorn 프로세스를 종료한 뒤
`/Users/runner/pr-review/scripts/local_review_env.sh`를 읽어 서버를 포그라운드로 다시 띄웁니다.

### 포그라운드 수동 재시작 (LaunchAgent 미사용 시)

```bash
pkill -f '/Users/runner/pr-review/venv/bin/uvicorn' || true

PY311="$(brew --prefix python@3.11)/bin/python3.11"
PYTHON_BIN="$PY311" ./scripts/install_local_review.sh /Users/runner/pr-review

CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export HOST=127.0.0.1
export PORT=8000
export GITHUB_TOKEN=ghp_xxx
export GITHUB_WEBHOOK_SECRET=replace-me
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
# export MLX_DEVICE=cpu
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
export DRY_RUN=1
zsh /Users/runner/pr-review/scripts/run_webhook_server.sh
```

운영 중 실제 리뷰를 남길 때는 마지막 시작 전에 `unset DRY_RUN`만 하면 됩니다.

### LaunchAgent로 상시 실행

터미널에서 `nohup ... &`로 띄운 프로세스는 세션 종료나 프로세스 그룹 정리에 같이 내려갈 수 있습니다.
운영에서는 LaunchAgent로 webhook 서버와 remote MLX generate 서버를 유지하는 편이 안전합니다.

기본 템플릿은 `/Users/runner` 경로를 기준으로 합니다. 다른 계정이나 설치 경로를 쓰면 plist 안의
`/Users/runner/pr-review`, `/Users/runner/mlx-final-py` 값을 실제 경로로 바꾼 뒤 설치하세요.

```bash
mkdir -p ~/Library/LaunchAgents

cp deploy/launchagents/com.swiftman.pr-review.plist ~/Library/LaunchAgents/
cp deploy/launchagents/com.swiftman.mlx-final-text.plist ~/Library/LaunchAgents/

launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.swiftman.pr-review.plist 2>/dev/null || true
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.swiftman.mlx-final-text.plist 2>/dev/null || true

launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.swiftman.mlx-final-text.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.swiftman.pr-review.plist

curl http://127.0.0.1:8002/healthz
curl http://127.0.0.1:8000/healthz
```

로그는 아래 파일에서 확인합니다.

```bash
tail -f /tmp/mlx-final-text.log /tmp/mlx-final-text.err.log
tail -f /tmp/mlx-pr-review-webhook.log /tmp/mlx-pr-review-webhook.err.log
```

## 7. 수동 웹훅 테스트 완전판

PR이 이미 열려 있다면 GitHub UI에서 다시 열고 닫지 않아도 서명된 웹훅을 직접 보내서 테스트할 수 있습니다.
테스트 스크립트는 [`scripts/send_test_webhook.sh`](/Users/m4_25/develop/codereview/scripts/send_test_webhook.sh)입니다.

### 7-1. `ping`으로 연결 확인

```bash
export GITHUB_WEBHOOK_SECRET=replace-me
export WEBHOOK_EVENT=ping
export WEBHOOK_URL=http://127.0.0.1:8000/github/webhook
zsh /Users/runner/pr-review/scripts/send_test_webhook.sh
```

### 7-2. `pull_request` 이벤트 수동 전송

```bash
export GITHUB_WEBHOOK_SECRET=replace-me
export GITHUB_REPOSITORY=swift-man/review.gorani.me
export PULL_NUMBER=1
export PR_ACTION=synchronize
export WEBHOOK_URL=http://127.0.0.1:8000/github/webhook
zsh /Users/runner/pr-review/scripts/send_test_webhook.sh
```

### 7-3. 실제 GitHub PR에 한글 종합 코멘트 + 라인 코멘트 남기기

모델이 실제로 이슈를 못 찾으면 라인 코멘트가 0개일 수 있습니다.
GitHub Review API 연동이 정상인지 확실히 검증하려면 테스트용 클라이언트로 한글 코멘트를 강제로 생성한 뒤 `DRY_RUN` 없이 웹훅을 보내면 됩니다.

```bash
pkill -f '/Users/runner/pr-review/venv/bin/uvicorn' || true

CERT_PATH="$(
  /Users/runner/pr-review/venv/bin/python -c 'import certifi; print(certifi.where())'
)"

export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export HOST=127.0.0.1
export PORT=8000
export GITHUB_TOKEN=ghp_xxx
export GITHUB_WEBHOOK_SECRET=replace-me
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mock_review_client"
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
unset DRY_RUN
zsh /Users/runner/pr-review/scripts/run_webhook_server.sh
```

다른 터미널에서:

```bash
export GITHUB_WEBHOOK_SECRET=replace-me
export GITHUB_REPOSITORY=swift-man/review.gorani.me
export PULL_NUMBER=1
export PR_ACTION=synchronize
export WEBHOOK_URL=http://127.0.0.1:8000/github/webhook
zsh /Users/runner/pr-review/scripts/send_test_webhook.sh
```

이 테스트가 성공하면 PR 타임라인에 한글 종합 코멘트가 1개 달리고, PR 상세 diff에는 한글 라인 코멘트가 1개 달립니다.
검증이 끝나면 `MLX_REVIEW_CMD`를 다시 실제 MLX 클라이언트로 되돌리고 서버를 재시작합니다.

설명:

- `GITHUB_WEBHOOK_SECRET`는 GitHub Webhook 설정의 Secret과 같은 값이어야 합니다.
- `GITHUB_REPOSITORY`는 반드시 `OWNER/REPO` 형식이어야 합니다.
- `PR_ACTION`은 `opened`, `synchronize`, `reopened`, `ready_for_review` 중 하나를 씁니다.
- `WEBHOOK_URL`은 서버를 띄운 `HOST`와 `PORT`에 맞춰야 합니다.
- 서버가 `DRY_RUN=1`로 떠 있으면 GitHub 리뷰는 실제로 등록되지 않고 로그만 남습니다.

### 7-4. 결과 해석

- `HTTP 202`: 웹훅 수신 성공, 백그라운드 처리 시작
- `HTTP 401 Invalid webhook signature`: 서버의 `GITHUB_WEBHOOK_SECRET`와 테스트 스크립트의 값이 다름
- `HTTP 000` 또는 `curl: (7)`: 서버가 `HOST:PORT`에서 떠 있지 않음

서버 로그 예시:

```json
[delivery=manual-1773424393] {"status": "completed", "repository": "swift-man/review.gorani.me", "pull_number": 1, "event": "COMMENT", "comment_count": 0, "payload": {"body": "No actionable issues found. The change is focused, easy to follow, and looks solid overall.\n\nNo actionable issues were identified in the reviewed diff.", "event": "COMMENT", "comments": []}}
```

## 8. Nginx 프록시

샘플 설정은 [`deploy/nginx-pr-review.conf`](/Users/m4_25/develop/codereview/deploy/nginx-pr-review.conf)에 있습니다.
`/github/webhook`와 `/healthz`만 FastAPI로 프록시하면 됩니다.

## 9. 웹훅 처리 흐름

[`review_runner/webhook_app.py`](/Users/m4_25/develop/codereview/review_runner/webhook_app.py)와 [`review_runner/review_service.py`](/Users/m4_25/develop/codereview/review_runner/review_service.py)는 다음을 수행합니다.

1. `POST /github/webhook` 수신
2. `X-Hub-Signature-256` 서명 검증
3. `pull_request` 이벤트와 허용 액션만 통과
4. GitHub API `pulls/{number}/files`로 파일 목록과 patch 조회
5. PR HEAD의 `.reviewbot.yml`이 있으면 `include` / `exclude` / `always_review` 규칙으로 리뷰 대상 파일을 필터링
6. patch와 최신 PR HEAD의 변경 파일 current code context를 MLX 프롬프트 JSON으로 직렬화
7. 공유된 MLX 실행 슬롯을 잡고 모델을 재사용해 JSON 응답 생성
8. MLX JSON 응답 검증
9. GitHub Review API payload로 변환
10. 라인 코멘트와 전체 리뷰를 한 번에 등록

### 9-1. 리뷰 대상 필터 설정

리뷰 대상 저장소 루트에 `.reviewbot.yml`을 두면 불필요한 파일을 프롬프트에서 제외할 수 있습니다. 설정 파일은 최신 PR HEAD 기준으로 읽으며, 파일이 없거나 파싱할 수 없으면 기존처럼 모든 patchable file을 리뷰합니다.

```yaml
version: 1

review:
  include:
    - "**/*.swift"
    - "Package.swift"
    - "Project.swift"
    - "Tuist/**/*.swift"
    - "**/Info.plist"

  exclude:
    - "Pods/**"
    - "Carthage/**"
    - ".build/**"
    - "DerivedData/**"
    - "build/**"
    - "dist/**"
    - "node_modules/**"
    - "vendor/**"
    - "**/Generated/**"
    - "**/*.generated.swift"
    - "**/*.pb.swift"
    - "**/*.graphql.swift"
    - "**/*+Generated.swift"
    - "**/*.xcassets/**"
    - "**/*.png"
    - "**/*.jpg"
    - "**/*.jpeg"
    - "**/*.gif"
    - "**/*.webp"
    - "**/*.pdf"
    - "**/*.mp4"
    - "**/*.mov"
    - "Package.resolved"
    - "Podfile.lock"
    - "package-lock.json"
    - "yarn.lock"
    - "pnpm-lock.yaml"
    - "README.md"
    - "CHANGELOG.md"
    - "docs/**"
    - "**/*.md"

  always_review:
    - ".reviewbot.yml"
    - "AGENTS.md"
    - "Package.swift"
    - "Project.swift"
    - "Tuist/**/*.swift"
```

적용 순서는 `always_review`가 최우선이고, 그 다음 `include`, 마지막으로 `exclude`입니다. 예를 들어 `**/*.md`를 제외하더라도 `always_review`에 `AGENTS.md`를 넣으면 해당 파일은 계속 리뷰됩니다. 또한 `.reviewbot.yml`과 `AGENTS.md`는 설정 파일이 실수나 악의로 제외하더라도 diff에 포함되어 있으면 항상 리뷰 대상에 남습니다.

## 10. CLI 테스트

기존 CLI 테스트도 유지됩니다. [`review_runner/review_pr.py`](/Users/m4_25/develop/codereview/review_runner/review_pr.py)는 다음을 수행합니다.

1. `GITHUB_EVENT_PATH`에서 PR 번호를 읽음
2. GitHub API `pulls/{number}/files`로 파일 목록과 patch를 읽음
3. PR HEAD의 `.reviewbot.yml`이 있으면 리뷰 대상 파일을 필터링함
4. 각 파일의 RIGHT-side comment 가능 라인을 계산함
5. patch와 최신 PR HEAD의 변경 파일 current code context를 MLX 프롬프트 JSON으로 직렬화함
6. MLX JSON 응답을 검증함
7. GitHub Review API payload로 변환함
8. 라인 코멘트와 전체 리뷰를 한 번에 등록함

## 11. MLX 어댑터 교체 포인트

실제 MLX 어댑터는 [`review_runner/mlx_review_client.py`](/Users/m4_25/develop/codereview/review_runner/mlx_review_client.py)입니다.
이 모듈은 `mlx-lm`으로 모델을 로드하고, stdin으로 받은 PR diff와 current code context payload를 chat prompt로 변환한 뒤,
아래 JSON 형식만 stdout으로 내보냅니다.

```json
{
  "summary": "The diff introduces one likely regression.",
  "event": "REQUEST_CHANGES",
  "positives": [],
  "must_fix": [],
  "suggestions": [],
  "comments": [
    {
      "path": "src/app.py",
      "line": 42,
      "severity": "Major",
      "confidence": 0.92,
      "body": "Problem: ... Why it matters: ... Suggested fix: ... Confidence: High"
    }
  ]
}
```

`severity`는 `Blocking`, `Major`, `Minor`, `Suggestion` 중 하나여야 하고, `line`은 반드시 해당 patch의
RIGHT-side 유효 라인이어야 합니다. 모델이 만든 finding은 `comments[]`에만 넣고,
`must_fix`와 `suggestions`는 빈 배열로 둡니다. 서비스는 path/line/confidence가 없는 top-level finding을
false positive 방지를 위해 게시하지 않습니다. `Blocking`/`Major`는 본문 `Confidence: High`와 numeric
`confidence >= 0.8`을 모두 만족할 때만 게시됩니다.

프롬프트의 `files[]`에는 GitHub diff patch와 함께 `current_file_context`가 들어갑니다. 작은 변경 파일은
최신 PR HEAD의 전체 파일을 line-numbered 형태로 넣고, 큰 파일은 모든 hunk 주변을 보존하도록 반경을 줄인
excerpt를 넣습니다. 명시적 `full` 모드에서 최대 길이 때문에 잘린 파일은 `full_file_truncated`로 표시합니다.
기본 `full_repo` 모드에서는 변경 파일 외 repo 파일도 `.reviewbot.yml`/built-in 필터와
Apple Silicon 64GB급 운영 예산 안에서 `repository_context`로 추가합니다. 모델은 이 context로 diff 밖 호출자와 helper 흐름을 검증하지만,
GitHub Review API 제약 때문에 실제 코멘트 line은 여전히 `valid_comment_lines` 안에서만 선택해야 합니다.
[`review_runner/sample_mlx_client.py`](/Users/m4_25/develop/codereview/review_runner/sample_mlx_client.py)는
기존 경로 호환을 위한 래퍼만 남겨뒀습니다.

## 12. GitHub Webhook 설정

GitHub 저장소 Settings -> Webhooks에서 아래처럼 연결하면 됩니다.

- Payload URL: `https://your-domain.example/github/webhook`
- Content type: `application/json`
- Secret: `GITHUB_WEBHOOK_SECRET`와 같은 값
- Events: `Pull requests`

## 13. 로컬 dry run

```bash
export GITHUB_TOKEN=ghp_xxx
export GITHUB_REPOSITORY=OWNER/REPO
export GITHUB_EVENT_PATH=/path/to/event.json
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
export MLX_MODEL="mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
export DRY_RUN=1
export PYTHONPATH=/Users/runner/pr-review
/Users/runner/pr-review/venv/bin/python -m review_runner.review_pr
```

GitHub App bot으로 dry run을 하고 싶다면 `GITHUB_TOKEN` 대신 아래처럼 App 환경 변수를 export 하면 됩니다.

```bash
export GITHUB_APP_ID=123456
export GITHUB_APP_PRIVATE_KEY_PATH=/Users/runner/pr-review/github-app.private-key.pem
export GITHUB_APP_INSTALLATION_ID=12345678
```

## 14. 자주 만나는 오류

### `zsh: command not found: pip`

`pip` 대신 venv 안의 Python으로 실행합니다.

```bash
/Users/runner/pr-review/venv/bin/python -m pip install -r /Users/runner/pr-review/review_runner/requirements.txt
```

### `zsh: command not found: python3.11`

Python 3.11이 아직 없다는 뜻입니다.

```bash
brew install python@3.11
```

### `{"detail":"Invalid webhook signature"}`

서버를 띄운 셸의 `GITHUB_WEBHOOK_SECRET`와 테스트를 보내는 셸의 `GITHUB_WEBHOOK_SECRET`가 다릅니다.
둘 다 같은 값으로 맞춘 뒤 서버를 다시 띄웁니다.

### `CERTIFICATE_VERIFY_FAILED`

대개 서버가 최신 코드로 동기화되지 않았거나, `SSL_CERT_FILE`과 `GITHUB_CA_BUNDLE`가 비어 있거나 잘못된 경로를 가리킬 때 발생합니다.
`install_local_review.sh`를 다시 실행한 뒤, `certifi.where()` 경로를 `SSL_CERT_FILE`과 `GITHUB_CA_BUNDLE`에 export 해서 서버를 재시작합니다.

### `mlx-lm is not installed`

의존성이 설치되지 않았거나 `MLX_REVIEW_CMD`가 venv Python이 아닌 시스템 `python3`를 가리킬 때 발생합니다.
아래 두 줄이 모두 성공해야 합니다.

```bash
/Users/runner/pr-review/venv/bin/python -m pip install -r /Users/runner/pr-review/review_runner/requirements.txt
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
```

### `Abort trap: 6`, `SIGABRT`, `libmlx.dylib`, `com.Metal.CompletionQueueDispatch`

이 조합은 대개 Python 레벨 예외가 아니라 MLX/Metal 쪽 네이티브 abort입니다. 특히 crash report에
`mlx::core::gpu::check_error(MTL::CommandBuffer*)`가 보이면 GPU command buffer 완료 시점에서
MLX가 실패를 감지하고 프로세스를 중단한 경우에 가깝습니다.

우선 아래처럼 CPU fallback으로 같은 요청이 통과하는지 확인합니다.

```bash
export MLX_DEVICE=cpu
export MLX_REVIEW_CMD="/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client"
```

`MLX_DEVICE=cpu`에서 안정적으로 동작하면 애플리케이션 로직보다는 해당 Mac의 Metal/driver/MLX 조합 문제일 가능성이 큽니다.
이 저장소의 MLX 클라이언트는 `MLX_DEVICE=cpu|gpu`를 읽으며, warm-up 결과에도 선택된 device가 함께 출력됩니다.

## 15. 7B 모델 전용 품질 보정 레이어 (모델 업그레이드 시 제거 대상)

로컬 클라이언트 코드 기본값인 `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` 는 PR diff 를 정확히 읽지 못하고 다음 세 가지 실패 패턴을 반복합니다. 운영 env 예시는 30B 모델을 지정하지만, 이 보정 레이어는 회귀 테스트로 제거 가능성이 확인될 때까지 유지합니다. 향후 14B 이상 모델만 쓰는 구성이 확정되면 **§15-5 에 명시된 역순(C → B → A)으로 제거하고 회귀 테스트를 돌려 유지 여부를 결정**하세요. 문서 배치 순서(A → B → C)는 설명의 논리 흐름이고, 실제 제거 순서는 바깥 계층부터입니다.

> 📌 **위치 탐색 안내**: 아래 표의 심볼명이 `review_runner/` 디렉터리 내 어디에 있는지는 `rg <symbol> review_runner/` (또는 ripgrep 이 없으면 `git grep <symbol> review_runner/`) 로 즉시 찾을 수 있습니다. 라인 번호는 코드 변경에 따라 drift 하므로 이 문서에서는 심볼명만 유지합니다.

### 15-1. 보정 대상 실패 패턴

1. **패턴 1 — 역해석**: diff 의 `+` 라인을 "missing" 으로 해석. 예: `"$schema": "./schema.json"` 을 추가하는 PR 에 대해 "`$schema` 필드가 추가되어야 합니다" 라는 must_fix 를 남김.
2. **패턴 2 — 환각**: 존재하지 않는 사실 단언. 예: 파일에 `$schema` 키가 1개뿐인데 "중복된 `$schema` 키가 있습니다" 라고 지적.
3. **패턴 3 — 중복 출력**: 같은 문장을 `positives` / `must_fix` / `suggestions` / `comments[]` 네 섹션에 그대로 반복.

회귀 검증용 실제 PR 케이스:
- `swift-man/MaterialDesignColor` PR #4 (commit `0fc8a67`) — 패턴 1 재현 (tokens/material-colors.json 의 `$schema` 추가)
- `swift-man/MaterialDesignColor` PR #4 — 패턴 2 재현 (중복 `$schema` 환각)
- `swift-man/MaterialDesignColor` PR #7 (commit `7c3a0d6`, `40a11fe`) — 패턴 1·3 동시 재현
- 같은 PR 의 ESM/CJS 이슈 (commit `38c8d92` 로 수정) — 차단성 이슈를 **정확히 잡는지** 확인용 positive control

### 15-2. A 계층 — 프롬프트 가드레일 (`review_runner/mlx_review_prompt.py`)

`SYSTEM_PROMPT_RULES` 내부에 명시된 규칙들로, 모델에게 직접 "이런 패턴을 내지 말라"고 지시.

| 규칙 위치 | 역할 |
|---------|------|
| `Anti-hallucination guardrails` 섹션 전체 | 리뷰 생성 전 self-check + 번역 금지 + add-already-exists 금지 + confidence gradient |
| `Do not restate what the diff already does` 규칙 | narration 금지 (패턴 3 일부) |
| `~가 추가되었습니다 / 변경되었습니다 / 수정되었습니다 are narration` | 서술형 어미 식별 |
| `Severity levels for comments[]` + severity/confidence enforcement | Blocking/Major 남용 억제 |

**제거 기준**: 15-1 의 회귀 PR 4 개 케이스를 새 모델로 돌려 다음 두 조건을 모두 만족하면 해당 규칙 제거.
1. 세 패턴(역해석 / 환각 / 중복 출력) 이 **한 건도 재현되지 않음**.
2. Blocking / Major 등급이 **재현 조건, 영향, 수정 방법, Confidence: High 없이 남발되지 않음**. 위 표에 `severity/confidence enforcement` 가 A 계층 책임으로 들어가 있으므로 이 조건이 빠지면 severity 오남용을 감지할 도구가 사라진다.

샘플 확장 시 주의: 기본 `MLX_TEMPERATURE=0.0` 에서는 같은 입력이 항상 같은 출력을 내므로 **단순 반복 실행은 샘플 다양성을 늘리지 못한다**. 샘플을 늘리려면 (1) `MLX_TEMPERATURE` 를 올려 비결정 샘플링을 활성화하거나, (2) 서로 다른 PR / commit fixture 를 15-1 목록에 추가하는 방향으로 간다. 어느 쪽이든 판정 기준은 여전히 "재현 0 건" 및 "오남용 0 건".

### 15-3. B 계층 — 후처리 필터 (`review_runner/review_service.py`)

프롬프트가 실패해도 출력 단에서 걸러내는 규칙 기반 필터. 상수 + `looks_like_*` 함수 쌍으로 구성.

| 상수 / 함수 | 역할 |
|-----------|------|
| `LOW_SIGNAL_POSITIVE_MARKERS` | "PR diff가 잘 작성되었습니다" 류 저신호 칭찬 |
| `LOW_SIGNAL_MODEL_CHANGE_MARKERS` | "MLX_MODEL 값이 변경" 류 단순 변경 narration |
| `PROCESS_POLICY_MARKERS` | PR 제목/description/AGENTS.md 같은 코드 외 정책 지적 |
| `POSITIVE_CONCERN_MARKERS` | "가독성을 높", "개선되었습니다" 류 positive-shaped concern |
| `NO_CONCERN_TEXTS` | "개선이 필요한 점은 없습니다" 같은 placeholder 차단 |
| `PROMPT_ECHO_MARKERS` | 프롬프트 본문을 finding 으로 재진술 |
| `DESCRIPTIVE_NARRATION_SUFFIXES` | "~되었습니다" 로 끝나는 서술형 `must_fix` / `suggestions` 항목 |
| `CONCERN_RISK_MARKERS` | 서술형 어미 허용 여부 판단용 위험 어휘 whitelist |
| `looks_like_praise_only_comment` | 칭찬만 있는 line comment 차단 |
| `looks_like_prompt_echo` / `looks_like_diff_stat_dump` / `looks_like_generic_positive` / `looks_like_generic_model_change_comment` / `looks_like_process_policy_comment` / `looks_like_descriptive_change_narration` / `looks_like_positive_only_concern` / `looks_like_identifier_localization_comment` / `looks_like_no_findings_summary` | 위 marker 들을 각 지적 유형에 적용하는 판정 함수들 |
| `split_legacy_concerns` | 구 스키마 `concerns` 를 risk marker 기준으로 `must_fix` / `suggestions` 분배 |

> ⚠️ **`normalize_severity` 는 B 계층 marker filter 와 분리**: `normalize_severity` 는 'critical/blocker/high/low/nit' 같은 관용어를 canonical severity enum 으로 정규화하는 **호환 레이어** 이지, 실패 패턴을 걸러내는 필터가 아닙니다. 아래 B 계층 제거 기준으로 판단하지 말고, **새 모델이 항상 Canonical severity(Blocking/Major/Minor/Suggestion)만 emit 한다고 확인된 뒤에만** 제거하세요. 조기 제거 시 관용어가 전부 Minor 로 폴백돼 event 라우팅이 왜곡됩니다.

**제거 기준**: 회귀 PR 4 개 케이스를 **후처리 필터를 비활성화한 상태** 로 새 모델에 돌렸을 때, **원본 모델 출력** 에서 해당 marker 가 겨냥하는 패턴이 한 건도 나타나지 않으면 해당 marker·함수 쌍 제거. 예를 들어 `LOW_SIGNAL_POSITIVE_MARKERS` 의 제거는 원본 출력의 positives 배열에 "PR diff 가 잘 작성되었습니다" 류 저신호 칭찬이 없는지로 판정, `DESCRIPTIVE_NARRATION_SUFFIXES` 는 `must_fix` / `suggestions` 에 "~되었습니다" 서술형 어미가 없는지로 판정.

비활성화는 **세 곳을 모두 bypass** 해야 B 계층이 완전히 꺼진다. 한 곳이라도 빠지면 해당 경로가 계속 필터링돼 판정이 낙관적으로 왜곡된다:
1. `validate_mlx_output` 내부의 `sanitize_text_items` / `sanitize_positive_items` — `must_fix` / `suggestions` / `positives` 필터링
2. `collect_validated_comments` 내부의 `looks_like_praise_only_comment` — `comments[]` 라인 코멘트 필터링
3. `validate_mlx_output` 내부의 `sanitize_summary` — `summary` 경로에서 `looks_like_prompt_echo` / `looks_like_diff_stat_dump` / `looks_like_generic_model_change_comment` / `looks_like_no_findings_summary` 필터링

제거 후 [tests/test_review_service.py](tests/test_review_service.py) 의 `DescriptiveChangeNarrationTests`, `SplitLegacyConcernsTests` 등 관련 테스트도 함께 삭제.

### 15-4. C 계층 — 구조적 검증 (Phase 4, 구현 예정)

프롬프트·marker 로 못 잡는 패턴 1·2 를 잡기 위한 symbol-based 검증.

| 예정 기능 | 대상 패턴 |
|---------|---------|
| `+` 라인 semantic 강화 프롬프트 | 1 |
| Finding 3요소 템플릿 (문제/영향/제안) | 3 |
| 4 섹션 중복 제거 후처리 (유사도 기반) | 3 |
| `verify_finding_against_diff` symbol 검증 (backtick/quoted 식별자 + intent verb) | 1·2 |
| Major/Minor 태그는 impact 명시 시만 허용 | severity 오남용 |

구현 완료 시 이 표를 해당 함수·프롬프트 블록에 대한 링크로 업데이트하세요.

**제거 기준**: 회귀 PR 4 개 케이스에서 새 모델이 dedup 후처리 없이 섹션별 유일한 응답을 내고, 모든 line-anchored 주장이 실제 diff 내용과 일치하면 C 계층 제거.

### 15-5. 제거 절차

1. 회귀용 테스트 픽스처 확인: `tests/fixtures/mlx_outputs/` 에 현재 세 패턴에 해당하는 샘플 출력이 저장돼 있는지 확인하고, 없으면 배포 로그에서 수집해 먼저 추가.
2. 제거 순서: C → B → A (바깥쪽 보정 먼저, 프롬프트는 마지막). 각 단계에서 `python3 -m unittest discover -s tests` 통과 확인.
3. B 계층 (후처리 필터) 판정은 **필터 비활성 원본 출력** 을 기준으로 한다. **세 곳** 을 모두 bypass 해야 완전 비활성:
   - `validate_mlx_output` 내부의 `sanitize_text_items` / `sanitize_positive_items` (`must_fix` / `suggestions` / `positives` 경로)
   - `collect_validated_comments` 내부의 `looks_like_praise_only_comment` (`comments[]` 라인 코멘트 경로)
   - `validate_mlx_output` 내부의 `sanitize_summary` (`summary` 경로)

   한 곳이라도 빠지면 해당 경로가 계속 필터링돼 판정이 낙관적으로 왜곡된다. 테스트 픽스처용으로 raw JSON 을 저장해 육안 비교 가능.
4. 실전 회귀: 위 15-1 의 4 개 PR 케이스를 새 모델로 돌려 세 패턴이 **한 건도 재현되지 않는지** 확인. 기본 `MLX_TEMPERATURE=0.0` 에서는 반복 실행이 같은 출력만 내므로, 샘플을 늘리려면 비결정 샘플링(`MLX_TEMPERATURE` 상향)을 켜거나 15-1 에 새 PR/commit fixture 를 추가한다. 어느 쪽이든 판정 기준은 "재현 0 건".
5. ESM/CJS positive control 통과도 함께 확인 (차단 이슈를 여전히 잡는지).

> 🛠️ **향후 개선 후보**: 위 bypass 가 매번 코드 수정이라 번거로우므로, `MLX_BYPASS_FILTERS=1` 환경변수로 세 경로(`sanitize_text_items` / `looks_like_praise_only_comment` / `sanitize_summary`) 를 한 번에 끄는 스위치를 별도 PR 로 도입하면 이 절차가 "환경변수 설정 → 재실행" 한 줄로 축소될 수 있다.

# review.gorani.me
