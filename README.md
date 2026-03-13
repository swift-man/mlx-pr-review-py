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
- GitHub Review API를 호출할 토큰
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
- `GITHUB_TOKEN=...`
- `GITHUB_WEBHOOK_SECRET=...`
- `MLX_REVIEW_CMD=/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client`
- `MLX_MODEL=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit`
- `GITHUB_API_URL=https://api.github.com` (옵션)
- `MLX_MAX_TOKENS=1200` (옵션)
- `MLX_MAX_FINDINGS=10` (옵션)
- `MLX_TRUST_REMOTE_CODE=0` (옵션)
- `DRY_RUN=1` (옵션, 실제 GitHub 리뷰를 남기지 않고 흐름만 확인할 때)

`GITHUB_WEBHOOK_SECRET`는 GitHub 저장소 Webhook 설정의 Secret과 반드시 같은 값이어야 합니다.
`GITHUB_REPOSITORY`는 수동 테스트 때 `swift-man/review.gorani.me`처럼 `OWNER/REPO` 형식이어야 합니다.
실서비스 리뷰는 `MLX_REVIEW_CMD=/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client`를 사용하고,
실제 GitHub Review API 연동만 검증할 때는 `review_runner.mock_review_client`로 바꿔서 테스트할 수 있습니다.

처음 요청에서 모델을 다운받게 하지 않으려면 미리 warm-up을 한 번 실행해두는 편이 좋습니다.

```bash
export LOCAL_REVIEW_HOME=/Users/runner/pr-review
export MLX_MODEL=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit
zsh /Users/runner/pr-review/scripts/warm_mlx_model.sh
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
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
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

코드 변경이 있거나 환경 변수를 바꿨다면 아래 순서대로 재시작하면 됩니다.

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
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
export SSL_CERT_FILE="$CERT_PATH"
export GITHUB_CA_BUNDLE="$CERT_PATH"
export DRY_RUN=1
zsh /Users/runner/pr-review/scripts/run_webhook_server.sh
```

운영 중 실제 리뷰를 남길 때는 마지막 시작 전에 `unset DRY_RUN`만 하면 됩니다.

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
5. patch를 MLX 프롬프트 JSON으로 직렬화
6. MLX JSON 응답 검증
7. GitHub Review API payload로 변환
8. 라인 코멘트와 전체 리뷰를 한 번에 등록

## 10. CLI 테스트

기존 CLI 테스트도 유지됩니다. [`review_runner/review_pr.py`](/Users/m4_25/develop/codereview/review_runner/review_pr.py)는 다음을 수행합니다.

1. `GITHUB_EVENT_PATH`에서 PR 번호를 읽음
2. GitHub API `pulls/{number}/files`로 파일 목록과 patch를 읽음
3. 각 파일의 RIGHT-side comment 가능 라인을 계산함
4. patch를 MLX 프롬프트 JSON으로 직렬화함
5. MLX JSON 응답을 검증함
6. GitHub Review API payload로 변환함
7. 라인 코멘트와 전체 리뷰를 한 번에 등록함

## 11. MLX 어댑터 교체 포인트

실제 MLX 어댑터는 [`review_runner/mlx_review_client.py`](/Users/m4_25/develop/codereview/review_runner/mlx_review_client.py)입니다.
이 모듈은 `mlx-lm`으로 모델을 로드하고, stdin으로 받은 PR diff payload를 chat prompt로 변환한 뒤,
아래 JSON 형식만 stdout으로 내보냅니다.

```json
{
  "summary": "The diff introduces one likely regression.",
  "event": "REQUEST_CHANGES",
  "comments": [
    {
      "path": "src/app.py",
      "line": 42,
      "body": "This branch now skips the None check and can raise an exception."
    }
  ]
}
```

`line`은 반드시 해당 patch의 RIGHT-side 유효 라인이어야 합니다.
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
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
export DRY_RUN=1
export PYTHONPATH=/Users/runner/pr-review
/Users/runner/pr-review/venv/bin/python -m review_runner.review_pr
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

# review.gorani.me
