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
- `MLX_REVIEW_CMD=/Users/runner/pr-review/venv/bin/python -m review_runner.mlx_review_client` (옵션)
- `MLX_MODEL=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit`
- `MLX_DEVICE=cpu` (옵션, Metal 장애 시 fallback. 비워두면 MLX 기본 장치 사용)
- `GITHUB_API_URL=https://api.github.com` (옵션)
- `MLX_MAX_TOKENS=1200` (옵션)
- `MLX_MAX_FINDINGS=10` (옵션)
- `MLX_TRUST_REMOTE_CODE=0` (옵션)
- `DRY_RUN=1` (옵션, 실제 GitHub 리뷰를 남기지 않고 흐름만 확인할 때)

`GITHUB_WEBHOOK_SECRET`는 GitHub 저장소 Webhook 설정의 Secret과 반드시 같은 값이어야 합니다.
`GITHUB_REPOSITORY`는 수동 테스트 때 `swift-man/review.gorani.me`처럼 `OWNER/REPO` 형식이어야 합니다.
GitHub App 인증을 쓰고 싶다면 `GITHUB_APP_ID`와 private key를 설정하면 되고, 이 경우 `GITHUB_TOKEN`보다 GitHub App installation token이 우선 사용됩니다.
GitHub App으로 인증하면 리뷰 작성자가 개인 계정이 아니라 App bot으로 보입니다.
`MLX_REVIEW_CMD`를 비워 두거나 기본값인 `python -m review_runner.mlx_review_client`를 쓰면
웹훅 서버 프로세스 안에서 MLX 모델을 재사용하고, 동시 리뷰 요청은 메모리 급증을 막기 위해 한 번에 하나씩 처리합니다.
실제 GitHub Review API 연동만 검증할 때는 `review_runner.mock_review_client` 같은 커스텀 커맨드로 바꿔서 테스트할 수 있습니다.

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
export MLX_MODEL=mlx-community/Qwen2.5-Coder-7B-Instruct-4bit
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
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
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
export MLX_MODEL="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
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

코드 변경이 있거나 환경 변수를 바꿨다면 아래 순서대로 재시작하면 됩니다.

한 번에 다시 배포하고 실행하려면 아래 래퍼 스크립트를 써도 됩니다.

```bash
./scripts/redeploy_local_review.sh /Users/runner/pr-review
```

이 스크립트는 기존 uvicorn 프로세스를 종료한 뒤, 최신 소스를 `/Users/runner/pr-review`로 다시 복사하고,
`/Users/runner/pr-review/scripts/local_review_env.sh`를 읽어 서버를 포그라운드로 다시 띄웁니다.

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
# export MLX_DEVICE=cpu
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
6. 공유된 MLX 실행 슬롯을 잡고 모델을 재사용해 JSON 응답 생성
7. MLX JSON 응답 검증
8. GitHub Review API payload로 변환
9. 라인 코멘트와 전체 리뷰를 한 번에 등록

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

현재 기본 모델인 `mlx-community/Qwen2.5-Coder-7B-Instruct-4bit` 는 PR diff 를 정확히 읽지 못하고 다음 세 가지 실패 패턴을 반복합니다. 이 패턴들을 **프롬프트 가드레일 + 후처리 필터** 로 막고 있으며, 향후 14B 이상 모델로 업그레이드하면 **아래 목록을 순서대로 제거하고 회귀 테스트를 돌려 유지 여부를 결정**하세요.

> 📌 **위치 탐색 안내**: 아래 표의 심볼명이 현재 파일에서 어디 있는지는 `rg <symbol> review_runner/` (또는 ripgrep 이 없으면 `git grep <symbol> review_runner/`) 로 즉시 찾을 수 있습니다. 라인 번호는 코드 변경에 따라 drift 하므로 이 문서에서는 심볼명만 유지합니다.

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
| `Severity levels for comments[]` + severity enforcement | Major/Minor 남용 억제 |

**제거 기준**: 15-1 의 회귀 PR 4 개 케이스를 새 모델로 돌려 다음 두 조건을 모두 만족하면 해당 규칙 제거.
1. 세 패턴(역해석 / 환각 / 중복 출력) 이 **한 건도 재현되지 않음**.
2. Critical / Major 등급이 **영향(impact) 서술 없이 남발되지 않음**. 위 표에 `severity enforcement` 가 A 계층 책임으로 들어가 있으므로 이 조건이 빠지면 severity 오남용을 감지할 도구가 사라진다.

샘플이 부족하다 판단되면 각 PR 을 반복 실행해 샘플을 확보하되 판정 기준은 여전히 "재현 0 건" 및 "오남용 0 건".

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
| `DESCRIPTIVE_NARRATION_SUFFIXES` | "~되었습니다" 로 끝나는 서술형 concern |
| `CONCERN_RISK_MARKERS` | 서술형 어미 허용 여부 판단용 위험 어휘 whitelist |
| `looks_like_praise_only_comment` | 칭찬만 있는 line comment 차단 |
| `normalize_severity` | 'blocker/high/low/nit' 같은 관용어 severity 정규화 |
| `looks_like_prompt_echo` / `looks_like_diff_stat_dump` / `looks_like_generic_positive` / `looks_like_generic_model_change_comment` / `looks_like_process_policy_comment` / `looks_like_descriptive_change_narration` / `looks_like_positive_only_concern` / `looks_like_identifier_localization_comment` / `looks_like_no_findings_summary` | 위 marker 들을 각 지적 유형에 적용하는 판정 함수들 |
| `split_legacy_concerns` | 구 스키마 `concerns` 를 risk marker 기준으로 `must_fix` / `suggestions` 분배 |

**제거 기준**: 회귀 PR 4 개 케이스를 **후처리 필터를 비활성화한 상태** 로 새 모델에 돌렸을 때, **원본 모델 출력** 에서 해당 marker 가 겨냥하는 패턴이 한 건도 나타나지 않으면 해당 marker·함수 쌍 제거. 예를 들어 `LOW_SIGNAL_POSITIVE_MARKERS` 의 제거는 원본 출력의 positives 배열에 "PR diff 가 잘 작성되었습니다" 류 저신호 칭찬이 없는지로 판정, `DESCRIPTIVE_NARRATION_SUFFIXES` 는 concerns/must_fix 에 "~되었습니다" 서술형 어미가 없는지로 판정.

비활성화는 **두 곳을 모두 bypass** 해야 B 계층이 완전히 꺼진다 (한쪽만 우회하면 라인 코멘트 경로가 계속 필터링됨):
1. `validate_mlx_output` 내부의 `sanitize_text_items` / `sanitize_positive_items` — must_fix / suggestions / positives 필터링
2. `collect_validated_comments` 내부의 `looks_like_praise_only_comment` — `comments[]` 라인 코멘트 필터링

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
3. B 계층 (후처리 필터) 판정은 **필터 비활성 원본 출력** 을 기준으로 한다. **두 곳** 을 모두 bypass 해야 완전 비활성:
   - `validate_mlx_output` 내부의 `sanitize_text_items` / `sanitize_positive_items` (must_fix / suggestions / positives 경로)
   - `collect_validated_comments` 내부의 `looks_like_praise_only_comment` (`comments[]` 라인 코멘트 경로)

   한쪽만 우회하면 라인 코멘트가 계속 걸러져 판정이 왜곡된다. 테스트 픽스처용으로 raw JSON 을 저장해 육안 비교 가능.
4. 실전 회귀: 위 15-1 의 4 개 PR 케이스를 새 모델로 돌려 세 패턴이 **한 건도 재현되지 않는지** 확인. 샘플이 부족하다 판단되면 각 PR 을 반복 실행해 수를 늘리되 판정 기준은 여전히 "재현 0 건".
5. ESM/CJS positive control 통과도 함께 확인 (차단 이슈를 여전히 잡는지).

> 🛠️ **향후 개선 후보**: 위 bypass 가 매번 코드 수정이라 번거로우므로, `MLX_BYPASS_FILTERS=1` 환경변수로 두 경로를 한 번에 끄는 스위치를 별도 PR 로 도입하면 이 절차가 "환경변수 설정 → 재실행" 한 줄로 축소될 수 있다.

# review.gorani.me
