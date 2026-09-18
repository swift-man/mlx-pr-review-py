# Changelog

이 문서는 `mlx-pr-review-py`의 사용자에게 보이는 주요 변경 사항을 기록합니다.

## [Unreleased]

### Added

- webhook 수신을 별도 repo [`pr-review-receiver`](https://github.com/swift-man/pr-review-receiver)로 분리하고, 이 repo는 Redis stream에서 job을 꺼내 리뷰하는 worker가 됐습니다. 리뷰가 프로세스 메모리가 아니라 Redis에 남아 worker를 재기동해도 유실되지 않고, worker를 여러 대 붙일 수 있습니다.
- `X-GitHub-Delivery` 기반 중복 제거를 추가했습니다. GitHub redelivery는 같은 GUID를 유지하므로 재전송이 중복 리뷰를 만들지 않습니다.
- worker가 죽어 ACK되지 않은 job은 `XAUTOCLAIM`이 회수합니다. 재시도 한도를 넘긴 job은 dead letter stream으로 격리해 회수 루프를 막지 않게 합니다.
- 큐에 있는 동안 같은 PR에 새 push가 들어온 job은 모델을 돌리기 전에 버립니다.

### Changed

- 기본 모델을 `mlx-community/Qwen3-Coder-Next-4bit`(80B total / 3B active, 코딩 특화)로 교체했습니다. 이전 기본값은 `Qwen2.5-Coder-7B-Instruct-4bit`였습니다.
- 리뷰 프롬프트를 증거 기준 중심으로 재작성했습니다. 7B 시절 누적된 금지 규칙 26개를 모든 지적이 통과해야 하는 증거 기준 하나로 접어 프롬프트가 10,480자에서 7,327자로 줄었습니다.
- 리뷰 confidence 문턱을 등급별로 나눴습니다. 머지를 막는 Blocking/Major는 0.8을 유지하고, 막지 않는 Minor/Suggestion은 0.6으로 낮춰 확신 0.6~0.8 구간의 정당한 지적이 버려지지 않게 했습니다.
- 유지보수·설계 관련 지적을 Minor/Suggestion 등급으로 허용합니다. 이전에는 금지돼 있었습니다.

### Fixed

- 외부 시스템 의미론(플랫폼 API, 설정 포맷, 프레임워크 생명주기)에 대한 주장은 제공된 컨텍스트로 입증되지 않으면 Suggestion 등급을 넘지 못하게 했습니다. 실측에서 launchd `KeepAlive` 동작을 반대로 단언한 Major 오탐이 나온 데 따른 조치입니다.

## [1.0.0] - 2026-07-04

### Added

- GitHub pull request webhook을 받아 PR 파일을 수집하고 GitHub Review API로 리뷰를 등록하는 FastAPI 서버를 제공합니다.
- MLX 기반 로컬 리뷰와 remote generate endpoint 기반 리뷰 실행을 지원합니다.
- PR diff뿐 아니라 최신 PR HEAD 기준 변경 파일의 full code 또는 hunk 주변 excerpt를 함께 읽는 리뷰 컨텍스트 구성을 제공합니다.
- `.reviewbot.yml`의 include/exclude/always_review 규칙으로 불필요한 파일 리뷰를 줄일 수 있습니다.
- 대형 PR에서는 prompt 크기 제한을 넘지 않도록 변경 파일을 batch로 나눠 리뷰합니다.
- GitHub App installation token 인증, 토큰 갱신, post 직전 HEAD 재확인, stale delivery 차단을 지원합니다.
- GitHub Review API 일시 장애에 대한 post retry와 중복 리뷰 방지 검사를 제공합니다.
- Copilot 리뷰 요청은 월간 budget 파일을 기준으로 선택적으로 요청할 수 있습니다.
- 로컬 설치, 재배포, LaunchAgent 재시동, 헬스체크를 위한 운영 스크립트를 제공합니다.

### Changed

- webhook 운영 기본 리뷰 컨텍스트 모드는 `auto`입니다. 작은 변경 파일은 최신 PR HEAD의 line-numbered full code로 읽고, 큰 파일은 변경 hunk 주변 excerpt로 줄입니다.
- diff patch는 GitHub 코멘트 anchor뿐 아니라 큰 파일 excerpt 생성을 위한 hunk 범위 계산에도 사용합니다.

### Fixed

- 긴 리뷰 중 새 HEAD가 push되면 오래된 코드 기준 리뷰를 게시하지 않도록 방지합니다.
- 같은 HEAD에 대한 중복 webhook이 들어와도 이미 진행 중인 리뷰를 중복 실행하지 않도록 보강했습니다.
- GitHub App token 만료나 401 응답 시 token을 갱신해 리뷰 등록을 재시도합니다.
