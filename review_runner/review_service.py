#!/usr/bin/env python3
"""Shared PR review service used by CLI entrypoints and the webhook server."""

from __future__ import annotations

import base64
import binascii
import calendar
import contextlib
import fnmatch
import json
import os
import re
import signal
import shlex
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any

import certifi
import jwt

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_CA_BUNDLE_ENV = "GITHUB_CA_BUNDLE"
DEFAULT_NO_FINDINGS_SUMMARY = (
    "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
)
DEFAULT_FINDINGS_SUMMARY = "자동 리뷰에서 확인이 필요한 변경 사항이 발견되었습니다. 아래 코멘트와 개선점을 확인해 주세요."
REVIEWBOT_CONFIG_PATH = ".reviewbot.yml"
DEFAULT_MAX_MODEL_FINDINGS = 10
MAX_MODEL_FINDINGS_ENV = "MLX_MAX_FINDINGS"
CURRENT_FILE_CONTEXT_LINE_RADIUS_ENV = "MLX_REVIEW_CONTEXT_LINE_RADIUS"
CURRENT_FILE_CONTEXT_MAX_CHARS_ENV = "MLX_REVIEW_CONTEXT_MAX_CHARS"
DEFAULT_CURRENT_FILE_CONTEXT_LINE_RADIUS = 120
DEFAULT_CURRENT_FILE_CONTEXT_MAX_CHARS = 220_000
CURRENT_FILE_CONTEXT_MODE_ENV = "MLX_REVIEW_CONTEXT_MODE"
# 기본값은 변경 파일의 최신 PR HEAD 전체를 주는 full. diff 는 GitHub
# 코멘트 anchor 로만 쓰고, full_repo 는 변경 외 repo 파일까지 추가로 붙인다.
DEFAULT_CURRENT_FILE_CONTEXT_MODE = "full"
REVIEW_PROMPT_MAX_CHARS_ENV = "MLX_REVIEW_PROMPT_MAX_CHARS"
DEFAULT_REVIEW_PROMPT_MAX_CHARS = 220_000
REPOSITORY_CONTEXT_MAX_FILES_ENV = "MLX_REVIEW_REPO_CONTEXT_MAX_FILES"
REPOSITORY_CONTEXT_MAX_CHARS_ENV = "MLX_REVIEW_REPO_CONTEXT_MAX_CHARS"
REPOSITORY_CONTEXT_FILE_MAX_CHARS_ENV = "MLX_REVIEW_REPO_CONTEXT_FILE_MAX_CHARS"
REVIEW_CONTEXT_API_TIMEOUT_SECONDS_ENV = "MLX_REVIEW_CONTEXT_API_TIMEOUT_SECONDS"
DEFAULT_REPOSITORY_CONTEXT_MAX_FILES = 120
DEFAULT_REPOSITORY_CONTEXT_MAX_CHARS = 320_000
DEFAULT_REPOSITORY_CONTEXT_FILE_MAX_CHARS = 18_000
DEFAULT_REVIEW_CONTEXT_API_TIMEOUT_SECONDS = 20
COPILOT_REVIEW_REQUEST_ENV = "COPILOT_REVIEW_REQUEST"
COPILOT_REVIEWER_ENV = "COPILOT_REVIEWER"
COPILOT_REVIEW_MONTHLY_BUDGET_ENV = "COPILOT_REVIEW_MONTHLY_BUDGET"
COPILOT_REVIEW_REQUEST_COST_ENV = "COPILOT_REVIEW_REQUEST_COST"
COPILOT_REVIEW_BUDGET_FILE_ENV = "COPILOT_REVIEW_BUDGET_FILE"
COPILOT_REVIEW_PENDING_TTL_SECONDS_ENV = "COPILOT_REVIEW_PENDING_TTL_SECONDS"
COPILOT_REVIEW_API_TIMEOUT_SECONDS_ENV = "COPILOT_REVIEW_API_TIMEOUT_SECONDS"
DEFAULT_COPILOT_REVIEWER = "copilot"
DEFAULT_COPILOT_REVIEW_MONTHLY_BUDGET = 50
# GitHub announced a Copilot code review multiplier of 13 from 2026-06-01.
# Keep the default conservative; operators can lower this to 1 if their plan still bills that way.
DEFAULT_COPILOT_REVIEW_REQUEST_COST = 13
DEFAULT_COPILOT_REVIEW_PENDING_TTL_SECONDS = 10 * 60
DEFAULT_COPILOT_REVIEW_API_TIMEOUT_SECONDS = 10
_COPILOT_REVIEW_BUDGET_LOCK = threading.Lock()
FORCED_ALWAYS_REVIEW_PATTERNS = (REVIEWBOT_CONFIG_PATH, "AGENTS.md")
DEFAULT_REVIEWBOT_EXCLUDE_PATTERNS = (
    # dependency / build output
    "Pods/**",
    "Carthage/**",
    ".build/**",
    "DerivedData/**",
    "build/**",
    "dist/**",
    "node_modules/**",
    "vendor/**",
    # generated documentation / archives
    "*.doccarchive/**",
    "**/*.doccarchive/**",
    # generated source
    "**/Generated/**",
    "**/*.generated.swift",
    "**/*.pb.swift",
    "**/*.graphql.swift",
    "**/*+Generated.swift",
    # resources and binary artifacts
    "**/*.xcassets/**",
    "**/*.png",
    "**/*.jpg",
    "**/*.jpeg",
    "**/*.gif",
    "**/*.webp",
    "**/*.pdf",
    "**/*.mp4",
    "**/*.mov",
    # lock files
    "Package.resolved",
    "Podfile.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
)
NO_FINDINGS_SUMMARY_MARKERS = (
    DEFAULT_NO_FINDINGS_SUMMARY,
    "즉시 수정이 필요한 문제는 보이지 않습니다.",
    "검토할 만한 문제를 찾지 못했습니다.",
    "지적할 만한 문제는 보이지 않습니다.",
    "별도 개선 필요 사항은 발견되지 않았습니다.",
    "개선이 필요한 점은 발견되지 않았습니다.",
)
DEFAULT_NO_CONCERNS_TEXT = "이번 diff 기준으로 별도 개선 필요 사항은 발견되지 않았습니다."
LOW_SIGNAL_POSITIVE_MARKERS = (
    "pr diff가 잘 작성",
    "pr diff의 내용이 잘 정리",
    "변경 내용이 잘 정리",
    "모든 파일이 잘 수정",
)
LOW_SIGNAL_FALLBACK_POSITIVE_MARKERS = (
    "핵심 변경 의도가 diff 안에서 비교적 명확하게 드러납니다.",
    "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
)
LOW_SIGNAL_MODEL_CHANGE_MARKERS = (
    "mlx_model의 값이 변경",
    "mlx_model의 값이 업데이트",
    "새로운 모델이 적합한지 확인",
)
PROCESS_POLICY_MARKERS = (
    "pr 제목",
    "pr description",
    "리뷰 텍스트는 작업 흐름을 분석",
    "작업 흐름을 분석",
    "한글로 작성되어야",
    "한국어로 작성되어야",
    "한국어로 작성해야",
    "agents.md",
    "커밋 메시지",
)
POSITIVE_CONCERN_MARKERS = (
    "가독성을 높",
    "신뢰성을 높",
    "유지보수성을 높",
    "명확해졌",
    "단순해졌",
    "효율적으로 관리",
    "안정적으로 관리",
    "도움이 됩니다",
    # '도움이 될 것입니다' 류 future-positive 표현. 패턴 4 (예: 'npm 릴리즈 워크플로우는
    # 새로운 패키지의 출시를 자동화하는 데 도움이 될 것입니다') 의 Major 라인 코멘트가
    # 이 marker 로 looks_like_praise_only_comment 에서 자동 drop 된다.
    "도움이 될",
    "좋습니다",
    "적절합니다",
    "개선되었습니다",
)
NO_CONCERN_TEXTS = {
    DEFAULT_NO_CONCERNS_TEXT,
    "별도 개선 필요 사항은 발견되지 않았습니다.",
    "개선이 필요한 점은 발견되지 않았습니다.",
    "개선이 필요한 점은 없습니다.",
    # Phase 2 새 라벨('반드시 수정할 사항' / '권장 개선사항') 에 맞춰 모델이 생성할 법한
    # 플레이스홀더 문구도 함께 차단한다. 새 프롬프트는 빈 배열을 권장하지만,
    # 모델이 습관적으로 '~없습니다' 문장을 채워 넣는 경우를 안전망으로 거른다.
    "반드시 수정할 사항은 없습니다.",
    "반드시 수정할 사항은 발견되지 않았습니다.",
    "권장 개선사항은 없습니다.",
    "권장 개선사항은 발견되지 않았습니다.",
    "수정이 필요한 항목은 없습니다.",
}
COMMON_TYPO_FIXES = {
    ("sta", "uts"): "status",
    ("reposit", "roy"): "repository",
    ("pull", "_nub", "mer"): "pull_number",
    ("X-GitHub-", "Eevnt"): "X-GitHub-Event",
}

SECRET_LOG_RE = re.compile(r"\b(token|secret|password|passwd|api[_-]?key|authorization)\b", re.IGNORECASE)
LOG_CALL_RE = re.compile(r"\b(print|logging\.\w+|logger\.\w+)\s*\(")
DIFF_STAT_RE = re.compile(r"\d+\s*개\s*(?:추가|삭제|변경)")
HUNK_HEADER_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(?P<start>\d+)(?:,(?P<length>\d+))?\s+@@")
PROMPT_ECHO_MARKERS = (
    "review_runner/",
    "valid_comment_lines",
    "RIGHT-side",
    "response_schema",
    "style-only",
    "praise-only",
)
# MLX 출력 말미에 흔히 붙는 구두점. narration 어미 매칭 전에 strip 해서 '.', '!', '?', '~',
# '。' 등이 섞여도 동일하게 탐지되도록 한다. 프로덕션 필터와 테스트 픽스처 assertion 이
# 같은 소스를 공유하게 모듈 상수로 노출한다.
NARRATION_TRAILING_PUNCTUATION = " .!?~。"
# diff 가 수행한 구조 변경을 사실 서술로만 적은 concern 을 걸러내기 위한 어미 목록.
# concern 은 '문제 진술' 이어야 하고 '~되었습니다' 류 narration 은 문제가 아니라 변경 요약이다.
DESCRIPTIVE_NARRATION_SUFFIXES = (
    "추가되었습니다",
    "변경되었습니다",
    "수정되었습니다",
    "도입되었습니다",
    "교체되었습니다",
    "삭제되었습니다",
    "제거되었습니다",
    "업데이트되었습니다",
    "생성되었습니다",
    "갱신되었습니다",
    "반영되었습니다",
    "작성되었습니다",
    "적용되었습니다",
    "이동되었습니다",
    "전환되었습니다",
    "개편되었습니다",
    "번역되었습니다",
)
# 위 서술형 어미가 있어도 문장 어디든 아래 위험 신호가 있으면 실제 concern 일 가능성이 높다.
CONCERN_RISK_MARKERS = (
    "위험",
    "누락",
    "문제",
    "실패",
    "버그",
    "주의",
    "우려",
    "필요",
    "부족",
    "우회",
    "취약",
    "오류",
    "에러",
    "결함",
    "크래시",
    "미흡",
    "빠져",
    "놓치",
    "깨짐",
    "깨지",
)
CONCERN_BLOCKING_PROMOTION_MARKERS = (
    "위험",
    "누락",
    "실패",
    "버그",
    "우회",
    "취약",
    "오류",
    "에러",
    "결함",
    "크래시",
    "빠져",
    "놓치",
    "깨짐",
    "깨지",
)
LINE_ANCHORED_LEGACY_CONCERN_FIELDS = frozenset({"legacy_concerns", "concerns"})

_MLX_RUN_LOCK = threading.Lock()


def log_progress(prefix: str, message: str) -> None:
    """웹훅 처리 중간 단계를 한 줄 로그로 남긴다."""
    print(f"{prefix}{message}", flush=True)


def increment_reason(counter: dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def format_reason_counts(counter: dict[str, int]) -> str:
    if not counter:
        return "none"
    return ", ".join(f"{reason}={count}" for reason, count in sorted(counter.items()))


def default_mlx_review_command() -> list[str]:
    """별도 설정이 없으면 현재 서버와 같은 Python 인터프리터로 MLX 클라이언트를 실행한다."""
    return [sys.executable, "-m", "review_runner.mlx_review_client"]


def configured_mlx_review_command() -> list[str]:
    """환경변수에 지정된 MLX 리뷰 커맨드가 있으면 파싱하고, 없으면 기본 커맨드를 쓴다."""
    raw_command = os.environ.get("MLX_REVIEW_CMD")
    return shlex.split(raw_command) if raw_command else default_mlx_review_command()


def resolve_command_executable(command: list[str]) -> str:
    """PATH에 있는 실행 파일까지 포함해 실제 실행 경로를 정규화한다."""
    if not command:
        return ""
    executable = shutil.which(command[0]) or command[0]
    return os.path.realpath(executable)


def uses_inprocess_mlx_client(command: list[str]) -> bool:
    """기본 MLX 클라이언트는 subprocess 대신 같은 프로세스 안에서 직접 호출한다."""
    if len(command) != 3 or command[1:] != ["-m", "review_runner.mlx_review_client"]:
        return False
    return resolve_command_executable(command) == os.path.realpath(sys.executable)


def normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def normalize_text_list(value: Any, max_items: int = 5) -> list[str]:
    if isinstance(value, list):
        candidates = value
    elif isinstance(value, str):
        candidates = [value]
    else:
        candidates = []

    normalized_items: list[str] = []
    seen: set[str] = set()

    for item in candidates:
        text = normalize_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized_items.append(text)
        if len(normalized_items) >= max_items:
            break

    return normalized_items


def sanitize_text_items(items: list[str], max_items: int = 5) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()

    for item in items:
        text = normalize_text(item)
        if (
            not text
            or text in seen
            or text in NO_CONCERN_TEXTS
            or looks_like_prompt_echo(text)
            or looks_like_diff_stat_dump(text)
            or looks_like_positive_only_concern(text)
            or looks_like_identifier_localization_comment(text)
            or looks_like_generic_model_change_comment(text)
            or looks_like_process_policy_comment(text)
            or looks_like_descriptive_change_narration(text)
        ):
            continue
        seen.add(text)
        sanitized.append(text)
        if len(sanitized) >= max_items:
            break

    return sanitized


def sanitize_positive_items(items: list[str], max_items: int = 5) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()

    for item in items:
        text = normalize_text(item)
        if (
            not text
            or text in seen
            or looks_like_prompt_echo(text)
            or looks_like_diff_stat_dump(text)
            or looks_like_generic_positive(text)
            or looks_like_generic_model_change_comment(text)
            or looks_like_process_policy_comment(text)
        ):
            continue
        seen.add(text)
        sanitized.append(text)
        if len(sanitized) >= max_items:
            break

    return sanitized


def looks_like_praise_only_comment(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False

    if looks_like_identifier_localization_comment(normalized):
        return True

    if looks_like_positive_only_concern(normalized):
        return True

    if looks_like_generic_model_change_comment(normalized):
        return True

    if looks_like_process_policy_comment(normalized):
        return True

    if looks_like_generic_positive(normalized):
        return True

    if looks_like_descriptive_change_narration(normalized):
        return True

    lowered = normalized.lower()
    return any(
        marker in lowered
        for marker in LOW_SIGNAL_FALLBACK_POSITIVE_MARKERS
    )


def build_ssl_context() -> ssl.SSLContext:
    """GitHub API 호출에 사용할 CA 번들을 환경변수와 certifi에서 순서대로 찾는다."""
    cafile = os.environ.get(DEFAULT_CA_BUNDLE_ENV) or os.environ.get("SSL_CERT_FILE") or certifi.where()
    return ssl.create_default_context(cafile=cafile)


class GitHubApiError(RuntimeError):
    def __init__(self, *, method: str, url: str, status: int, response_body: str) -> None:
        self.method = method
        self.url = url
        self.status = status
        self.response_body = response_body
        super().__init__(f"GitHub API {method} {url} failed: {status} {response_body}")


def request_json_url(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    ssl_context: ssl.SSLContext | None = None,
    timeout: float | None = None,
) -> Any:
    """공통 GitHub API JSON 호출 래퍼다."""
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, context=ssl_context or build_ssl_context(), timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(method=method, url=url, status=exc.code, response_body=message) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            ca_bundle = os.environ.get(DEFAULT_CA_BUNDLE_ENV) or os.environ.get("SSL_CERT_FILE") or certifi.where()
            raise RuntimeError(
                "GitHub API TLS verification failed. "
                "Set SSL_CERT_FILE or GITHUB_CA_BUNDLE if you need a custom CA bundle. "
                f"Current CA bundle: {ca_bundle}"
            ) from exc
        raise


def build_github_headers(token: str, *, content_type: bool = True) -> dict[str, str]:
    """GitHub REST API 기본 헤더를 만든다."""
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "mac-mini-pr-reviewer",
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


def load_github_app_private_key() -> str:
    """GitHub App private key를 문자열 또는 파일 경로에서 읽어온다."""
    inline_key = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if inline_key:
        return inline_key.replace("\\n", "\n")

    key_path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if key_path:
        with open(key_path, "r", encoding="utf-8") as fh:
            return fh.read()

    raise RuntimeError("Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH for GitHub App authentication")


def build_github_app_jwt(app_id: str, private_key: str) -> str:
    """짧은 TTL의 GitHub App JWT를 만든다."""
    now = int(time.time())
    payload = {
        "iat": now - 60,
        "exp": now + 9 * 60,
        "iss": app_id,
    }
    token = jwt.encode(payload, private_key, algorithm="RS256")
    return str(token)


@dataclass
class ResolvedGitHubToken:
    token: str
    source: str
    installation_id: int | None = None


def resolve_github_app_installation_id(app_jwt: str, repository: str, api_url: str, ssl_context: ssl.SSLContext) -> int:
    """저장소 기준으로 GitHub App installation ID를 조회한다."""
    installation = request_json_url(
        "GET",
        f"{api_url.rstrip('/')}/repos/{repository}/installation",
        headers=build_github_headers(app_jwt, content_type=False),
        ssl_context=ssl_context,
    )
    try:
        return int(installation["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Could not resolve GitHub App installation ID for repository {repository}") from exc


def parse_installation_id(
    raw_installation_id: str | None,
    *,
    app_jwt: str,
    repository: str | None,
    api_url: str,
    ssl_context: ssl.SSLContext,
) -> int:
    """환경변수 또는 저장소 조회 결과를 이용해 installation ID를 결정한다."""
    if raw_installation_id:
        try:
            return int(raw_installation_id)
        except ValueError as exc:
            raise RuntimeError("GITHUB_APP_INSTALLATION_ID must be an integer") from exc

    if not repository:
        raise RuntimeError("GITHUB_APP_INSTALLATION_ID is required when the repository is not available for installation lookup")

    return resolve_github_app_installation_id(app_jwt, repository, api_url, ssl_context)


def request_installation_token(
    app_jwt: str,
    installation_id: int,
    *,
    api_url: str,
    ssl_context: ssl.SSLContext,
) -> str:
    """설치된 GitHub App을 대신할 installation token을 발급받는다."""
    response = request_json_url(
        "POST",
        f"{api_url.rstrip('/')}/app/installations/{installation_id}/access_tokens",
        headers=build_github_headers(app_jwt),
        body={},
        ssl_context=ssl_context,
    )
    token = str(response.get("token") or "").strip()
    if not token:
        raise RuntimeError("GitHub App installation token response did not include a token")
    return token


def resolve_github_token(repository: str | None = None, api_url: str = DEFAULT_API_URL) -> ResolvedGitHubToken:
    """GitHub App이 설정돼 있으면 App 인증을, 아니면 PAT를 우선 사용한다."""
    app_id = os.environ.get("GITHUB_APP_ID")
    if app_id:
        private_key = load_github_app_private_key()
        ssl_context = build_ssl_context()
        app_jwt = build_github_app_jwt(app_id, private_key)
        installation_id = parse_installation_id(
            os.environ.get("GITHUB_APP_INSTALLATION_ID"),
            app_jwt=app_jwt,
            repository=repository,
            api_url=api_url,
            ssl_context=ssl_context,
        )
        token = request_installation_token(
            app_jwt,
            installation_id,
            api_url=api_url,
            ssl_context=ssl_context,
        )
        return ResolvedGitHubToken(
            token=token,
            source="github_app_installation",
            installation_id=installation_id,
        )

    token = str(os.environ.get("GITHUB_TOKEN") or "").strip()
    if token:
        return ResolvedGitHubToken(token=token, source="personal_access_token")

    raise RuntimeError(
        "Set GITHUB_TOKEN or configure GitHub App authentication with GITHUB_APP_ID plus a private key"
    )


SEVERITY_BLOCKING = "Blocking"
# Backward-compatible alias: older model/test fixtures may still say Critical.
SEVERITY_CRITICAL = SEVERITY_BLOCKING
SEVERITY_MAJOR = "Major"
SEVERITY_MINOR = "Minor"
SEVERITY_SUGGESTION = "Suggestion"
ALL_SEVERITIES = (SEVERITY_BLOCKING, SEVERITY_MAJOR, SEVERITY_MINOR, SEVERITY_SUGGESTION)
MIN_MODEL_COMMENT_CONFIDENCE = 0.8
MAX_EXISTING_REVIEW_CONTEXT_ITEMS = 30
MAX_EXISTING_REVIEW_CONTEXT_BODY_CHARS = 900
MAX_COPILOT_REVIEW_SECTION_ITEMS = 5
MAX_COPILOT_REVIEW_SECTION_BODY_CHARS = 220
REVIEW_CONTEXT_ISSUE_COMMENT_SKIP_MARKERS = (
    "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->",
    "<!-- walkthrough_start -->",
    "<!-- internal state start -->",
)
FINDING_BODY_RE = re.compile(
    r"^\s*problem\s*:\s*(?P<problem>.+?)\s+"
    r"why\s+it\s+matters\s*:\s*(?P<why>.+?)\s+"
    r"suggested\s+fix\s*:\s*(?P<fix>.+?)\s+"
    r"confidence\s*:\s*(?P<confidence>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
CONFIDENCE_LABEL_RE = re.compile(r"^(high|medium|low)$", re.IGNORECASE)
# Blocking / Major 는 머지 전 반드시 봐야 하는 차단성 등급이라 event 를 REQUEST_CHANGES
# 로 승격시킨다. Minor / Suggestion 은 개선 제안이라 COMMENT 로 남는다.
BLOCKING_SEVERITIES = frozenset({SEVERITY_BLOCKING, SEVERITY_MAJOR})


def normalize_severity(value: Any) -> str:
    """모델이 실어 보낸 severity 값을 정규화된 4단계 중 하나로 변환한다.

    대소문자와 앞뒤 공백을 무시하고, 코드 리뷰 관용어(critical/high/low/nit 등)도
    가까운 등급으로 매핑한다. 인식 불가이거나 누락된 경우 Minor 로 폴백해 잘못된
    Blocking 승격을 막는다.
    """
    if not isinstance(value, str):
        return SEVERITY_MINOR
    cleaned = value.strip().lower()
    mapping = {
        # 공식 4단계
        "blocking": SEVERITY_BLOCKING,
        "critical": SEVERITY_BLOCKING,
        "major": SEVERITY_MAJOR,
        "minor": SEVERITY_MINOR,
        "suggestion": SEVERITY_SUGGESTION,
        # 관용어 동의어 — 모델이 학습 데이터에서 흡수한 대체 표현들을 안전하게 흡수한다.
        "blocker": SEVERITY_BLOCKING,
        "severe": SEVERITY_BLOCKING,
        "high": SEVERITY_MAJOR,
        "medium": SEVERITY_MINOR,
        "moderate": SEVERITY_MINOR,
        "low": SEVERITY_SUGGESTION,
        "nit": SEVERITY_SUGGESTION,
        "nitpick": SEVERITY_SUGGESTION,
        "optional": SEVERITY_SUGGESTION,
    }
    return mapping.get(cleaned, SEVERITY_MINOR)


def normalize_confidence(value: Any) -> float | None:
    """모델이 보낸 confidence 를 0.0~1.0 실수로 정규화한다.

    bool 은 Python 에서 int 의 하위 타입이라 명시적으로 거부한다. confidence 가 없거나
    범위를 벗어나면 모델 코멘트는 게시하지 않는다.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= confidence <= 1.0):
        return None
    return confidence


def has_required_finding_sections(body: str) -> bool:
    """모델 라인 코멘트가 요청된 리뷰 코멘트 템플릿을 모두 포함하는지 확인한다."""
    return FINDING_BODY_RE.match(body) is not None


def extract_confidence_label(body: str) -> str | None:
    """본문 마지막 Confidence: High/Medium/Low 라벨을 추출한다."""
    body_match = FINDING_BODY_RE.match(body)
    if body_match is None:
        return None
    label = body_match.group("confidence").strip().rstrip(NARRATION_TRAILING_PUNCTUATION).strip()
    label_match = CONFIDENCE_LABEL_RE.match(label)
    if label_match is None:
        return None
    return label_match.group(1).lower()


def extract_finding_problem(body: str) -> str:
    """구조화된 finding 본문에서 Problem 섹션만 꺼낸다."""
    body_match = FINDING_BODY_RE.match(body)
    if body_match is None:
        return normalize_text(body)
    return normalize_text(body_match.group("problem"))


def confidence_score_for_label(label: str | None) -> float | None:
    """본문 Confidence 라벨을 보수적인 numeric score 로 변환한다.

    top-level finding 복구 경로는 모델이 별도 numeric confidence 필드를 제공하지
    못하는 경우가 많다. 그래도 High 라벨과 line anchor 가 모두 있으면 0.9 로
    보수적으로 흡수하고, Medium/Low 는 기존 0.8 gate 를 넘지 못하게 둔다.
    """
    if label == "high":
        return 0.9
    if label == "medium":
        return 0.7
    if label == "low":
        return 0.5
    return None


def format_finding_body(
    *,
    problem: str,
    why_it_matters: str,
    suggested_fix: str,
    confidence: str = "High",
) -> str:
    """GitHub 라인 코멘트에 쓰는 표준 finding 본문을 만든다."""
    return (
        f"Problem: {normalize_text(problem)} "
        f"Why it matters: {normalize_text(why_it_matters)} "
        f"Suggested fix: {normalize_text(suggested_fix)} "
        f"Confidence: {confidence}"
    )


@dataclass
class ReviewComment:
    path: str
    line: int
    body: str
    severity: str = SEVERITY_MINOR
    confidence: float = 1.0
    side: str = "RIGHT"


@dataclass
class PullRequestFile:
    filename: str
    status: str
    patch: str
    additions: int
    deletions: int
    right_side_lines: set[int]
    current_file_context: str = ""
    current_file_context_mode: str = ""


@dataclass
class RepositoryContextEntry:
    path: str
    content: str
    mode: str = "full_file"


@dataclass(frozen=True, slots=True)
class ReviewContextSettings:
    mode: str
    line_radius: int
    max_chars: int
    repository_max_files: int
    repository_max_chars: int
    repository_file_max_chars: int
    api_timeout_seconds: int


@dataclass(frozen=True)
class PullRequestDiscussionItem:
    source: str
    author: str
    body: str
    comment_id: int | None = None
    path: str | None = None
    line: int | None = None
    reply_to_comment_id: int | None = None
    created_at: str = ""

    def to_prompt_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "author": self.author,
            "body": self.body,
        }
        if self.comment_id is not None:
            payload["comment_id"] = self.comment_id
        if self.path:
            payload["path"] = self.path
        if self.line is not None:
            payload["line"] = self.line
        if self.reply_to_comment_id is not None:
            payload["reply_to_comment_id"] = self.reply_to_comment_id
        if self.created_at:
            payload["created_at"] = self.created_at
        return payload


@dataclass(frozen=True)
class ReviewBotConfig:
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    always_review: tuple[str, ...] = ()
    loaded: bool = False

    @property
    def has_filters(self) -> bool:
        return bool(self.include or self.exclude or self.always_review)


@dataclass
class PullRequestFileLoadResult:
    files: list[PullRequestFile]
    patchable_count: int
    repository_context: list[RepositoryContextEntry] = field(default_factory=list)
    skipped_by_reviewbot: int = 0
    reviewbot_config_loaded: bool = False
    default_filter_applied: bool = False


@dataclass
class ValidatedReview:
    """모델 출력과 규칙 기반 검사를 합쳐서 정규화한 리뷰 결과다.

    must_fix: 버그·보안·누락 등 머지 전 반드시 고쳐야 하는 항목. 비어 있지 않으면
              event 는 REQUEST_CHANGES 로 강제된다.
    suggestions: 권장 개선 (nice-to-have). 있어도 REQUEST_CHANGES 는 아니다.
    positives: 이 PR 이 개선한 기술적 효과.
    """

    comments: list[ReviewComment]
    summary: str
    event: str
    positives: list[str]
    must_fix: list[str]
    suggestions: list[str]


@dataclass
class CommentValidationStats:
    raw_model_comments: int = 0
    accepted_model_comments: int = 0
    dropped_model_comment_reasons: dict[str, int] = field(default_factory=dict)
    raw_top_level_findings: int = 0
    accepted_top_level_findings: int = 0
    dropped_top_level_finding_reasons: dict[str, int] = field(default_factory=dict)
    rule_based_added: int = 0
    rule_based_duplicates: int = 0


@dataclass
class ReviewGenerationArtifacts:
    prompt: str
    mlx_result: dict[str, Any]
    validated_review: ValidatedReview
    payload: dict[str, Any]


@dataclass
class PostedReviewResult:
    response: Any
    posted_event: str
    payload: dict[str, Any]
    fallback_note: str
    requested_event: str | None = None


class GitHubApi:
    """PR 파일 조회와 리뷰 등록에 필요한 GitHub API 접근을 모은다."""

    def __init__(self, token: str, repository: str, api_url: str = DEFAULT_API_URL) -> None:
        self.token = token
        self.repository = repository
        self.api_url = api_url.rstrip("/")
        self.ssl_context = build_ssl_context()
        self._pull_head_sha_cache: dict[int, str] = {}

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        url = f"{self.api_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return request_json_url(
            method,
            url,
            headers=build_github_headers(self.token),
            body=body,
            ssl_context=self.ssl_context,
            timeout=timeout,
        )

    def list_pr_files(self, pull_number: int) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request_json(
                "GET",
                f"/repos/{self.repository}/pulls/{pull_number}/files",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def list_issue_comments(self, pull_number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request_json(
                "GET",
                f"/repos/{self.repository}/issues/{pull_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def list_review_comments(self, pull_number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self.request_json(
                "GET",
                f"/repos/{self.repository}/pulls/{pull_number}/comments",
                params={"per_page": 100, "page": page},
            )
            if not batch:
                break
            comments.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return comments

    def list_requested_reviewers(self, pull_number: int, *, timeout: float | None = None) -> list[dict[str, Any]]:
        response = self.request_json(
            "GET",
            f"/repos/{self.repository}/pulls/{pull_number}/requested_reviewers",
            timeout=timeout,
        )
        if not isinstance(response, dict):
            return []
        users = response.get("users")
        if isinstance(users, list):
            return [user for user in users if isinstance(user, dict)]
        return []

    def request_reviewers(self, pull_number: int, reviewers: list[str], *, timeout: float | None = None) -> Any:
        return self.request_json(
            "POST",
            f"/repos/{self.repository}/pulls/{pull_number}/requested_reviewers",
            body={"reviewers": reviewers},
            timeout=timeout,
        )

    def get_pull_head_sha(self, pull_number: int) -> str:
        cached_sha = self._pull_head_sha_cache.get(pull_number)
        if cached_sha:
            return cached_sha
        pull = self.request_json("GET", f"/repos/{self.repository}/pulls/{pull_number}")
        head = pull.get("head") if isinstance(pull, dict) else None
        sha = head.get("sha") if isinstance(head, dict) else None
        normalized_sha = str(sha or "").strip()
        if not normalized_sha:
            raise RuntimeError(f"GitHub pull request response did not include head.sha for #{pull_number}")
        self._pull_head_sha_cache[pull_number] = normalized_sha
        return normalized_sha

    def list_repo_tree(self, ref: str, *, timeout: float | None = None) -> list[dict[str, Any]]:
        response = self.request_json(
            "GET",
            f"/repos/{self.repository}/git/trees/{ref}",
            params={"recursive": "1"},
            timeout=timeout,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"GitHub tree response for {ref} was not an object")
        tree = response.get("tree")
        if not isinstance(tree, list):
            raise RuntimeError(f"GitHub tree response for {ref} did not include tree[]")
        return [item for item in tree if isinstance(item, dict)]

    def get_file_text(self, path: str, *, ref: str, timeout: float | None = None) -> str:
        encoded_path = urllib.parse.quote(path, safe="/")
        response = self.request_json(
            "GET",
            f"/repos/{self.repository}/contents/{encoded_path}",
            params={"ref": ref},
            timeout=timeout,
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"GitHub contents response for {path} was not a file")
        entry_type = response.get("type")
        if entry_type != "file":
            raise RuntimeError(f"GitHub contents response for {path} was not a regular file (type={entry_type!r})")
        encoding = response.get("encoding")
        content = response.get("content")
        if encoding != "base64" or not isinstance(content, str):
            raise RuntimeError(
                f"GitHub contents response for {path} did not include base64 content "
                f"(type={entry_type!r}, encoding={encoding!r})"
            )
        try:
            return base64.b64decode("".join(content.split()), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise RuntimeError(
                f"GitHub contents response for {path} could not be decoded as UTF-8 "
                f"(type={entry_type!r}, encoding={encoding!r})"
            ) from exc

    def post_review(self, pull_number: int, body: dict[str, Any]) -> Any:
        return self.request_json(
            "POST",
            f"/repos/{self.repository}/pulls/{pull_number}/reviews",
            body=body,
        )


def coerce_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def truncate_existing_review_context_body(value: Any) -> str:
    normalized = normalize_text(value)
    if len(normalized) <= MAX_EXISTING_REVIEW_CONTEXT_BODY_CHARS:
        return normalized
    return normalized[: MAX_EXISTING_REVIEW_CONTEXT_BODY_CHARS - 3].rstrip() + "..."


def github_comment_author(raw_comment: dict[str, Any]) -> str:
    user = raw_comment.get("user")
    if isinstance(user, dict):
        login = normalize_text(user.get("login"))
        if login:
            return login
    return "unknown"


def should_skip_issue_comment_context(body: Any) -> bool:
    if not normalize_text(body):
        return True
    raw_body = body if isinstance(body, str) else ""
    return any(marker in raw_body for marker in REVIEW_CONTEXT_ISSUE_COMMENT_SKIP_MARKERS)


def build_issue_comment_context(raw_comment: dict[str, Any]) -> PullRequestDiscussionItem | None:
    body = raw_comment.get("body")
    if should_skip_issue_comment_context(body):
        return None
    return PullRequestDiscussionItem(
        source="issue_comment",
        author=github_comment_author(raw_comment),
        body=truncate_existing_review_context_body(body),
        comment_id=coerce_optional_int(raw_comment.get("id")),
        created_at=normalize_text(raw_comment.get("created_at")),
    )


def build_review_comment_context(raw_comment: dict[str, Any]) -> PullRequestDiscussionItem | None:
    body = truncate_existing_review_context_body(raw_comment.get("body"))
    if not body:
        return None
    return PullRequestDiscussionItem(
        source="review_comment",
        author=github_comment_author(raw_comment),
        body=body,
        comment_id=coerce_optional_int(raw_comment.get("id")),
        path=normalize_text(raw_comment.get("path")) or None,
        line=coerce_optional_int(raw_comment.get("line")),
        reply_to_comment_id=coerce_optional_int(raw_comment.get("in_reply_to_id")),
        created_at=normalize_text(raw_comment.get("created_at")),
    )


def is_copilot_review_context_item(item: dict[str, Any]) -> bool:
    author = normalize_text(item.get("author")).lower()
    return "copilot" in author


def env_flag_enabled(name: str) -> bool:
    raw_value = normalize_text(os.environ.get(name)).lower()
    return raw_value in {"1", "true", "yes", "on", "auto"}


def parse_positive_int_env(name: str, default: int) -> int:
    raw_value = normalize_text(os.environ.get(name))
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def normalize_copilot_reviewer(value: str | None = None) -> str:
    reviewer = normalize_text(value if value is not None else os.environ.get(COPILOT_REVIEWER_ENV))
    if not reviewer:
        reviewer = DEFAULT_COPILOT_REVIEWER
    return reviewer.lstrip("@")


def default_copilot_review_budget_file() -> str:
    configured_path = normalize_text(os.environ.get(COPILOT_REVIEW_BUDGET_FILE_ENV))
    if configured_path:
        return os.path.expanduser(configured_path)

    local_home = normalize_text(os.environ.get("LOCAL_REVIEW_HOME"))
    if local_home:
        return os.path.join(local_home, ".copilot_review_budget.json")

    return os.path.expanduser("~/.mlx-pr-review-copilot-budget.json")


def load_copilot_review_budget_state(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    if not isinstance(state, dict):
        raise RuntimeError(f"Copilot review budget file must contain a JSON object: {path}")
    return state


def save_copilot_review_budget_state(path: str, state: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory or ".",
            prefix=f".{os.path.basename(path)}.",
            suffix=".tmp",
            delete=False,
        ) as fh:
            tmp_path = fh.name
            json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@contextlib.contextmanager
def locked_copilot_review_budget_state(path: str):
    lock_path = f"{path}.lock"
    directory = os.path.dirname(lock_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with _COPILOT_REVIEW_BUDGET_LOCK:
        with open(lock_path, "a+", encoding="utf-8") as lock_fh:
            if fcntl is not None:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)


def current_copilot_review_budget_month() -> str:
    return time.strftime("%Y-%m", time.gmtime())


def current_utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def parse_utc_timestamp_seconds(value: Any) -> int | None:
    normalized = normalize_text(value)
    if not normalized:
        return None
    try:
        return calendar.timegm(time.strptime(normalized, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return None


def get_copilot_request_status(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    return normalize_text(entry.get("status")).lower()


def is_recent_copilot_pending_request(entry: Any, *, ttl_seconds: int) -> bool:
    if get_copilot_request_status(entry) != "pending":
        return False
    requested_at_seconds = parse_utc_timestamp_seconds(entry.get("requested_at") if isinstance(entry, dict) else None)
    if requested_at_seconds is None:
        return False
    return time.time() - requested_at_seconds < ttl_seconds


def get_copilot_request_entry_cost(entry: Any, default: int) -> int:
    if not isinstance(entry, dict):
        return default
    cost = coerce_optional_int(entry.get("cost"))
    if cost is None or cost <= 0:
        return default
    return cost


def get_copilot_month_entry(state: dict[str, Any], month: str) -> dict[str, Any]:
    raw_entry = state.get(month)
    if not isinstance(raw_entry, dict):
        raw_entry = {}
        state[month] = raw_entry

    used = raw_entry.get("used")
    if not isinstance(used, int) or used < 0:
        raw_entry["used"] = 0

    requests = raw_entry.get("requests")
    if not isinstance(requests, dict):
        raw_entry["requests"] = {}

    return raw_entry


def get_copilot_request_history(state: dict[str, Any]) -> dict[str, Any]:
    raw_history = state.get("requests")
    if not isinstance(raw_history, dict):
        raw_history = {}
        state["requests"] = raw_history
    return raw_history


def record_copilot_review_budget_request(
    *,
    state: dict[str, Any],
    month_entry: dict[str, Any],
    request_key: str,
    cost: int,
    reviewer: str,
    month: str,
    status: str,
) -> dict[str, Any]:
    entry = {
        "cost": cost,
        "month": month,
        "requested_at": current_utc_timestamp(),
        "reviewer": reviewer,
        "status": status,
    }
    month_entry["used"] += cost
    get_copilot_request_history(state)[request_key] = entry
    month_entry["requests"][request_key] = entry
    return entry


def remove_copilot_review_budget_request(
    *,
    state: dict[str, Any],
    month_entry: dict[str, Any],
    request_key: str,
    cost: int,
) -> None:
    history_entry = get_copilot_request_history(state).pop(request_key, None)
    removed_from_month = False

    for raw_entry in state.values():
        if not isinstance(raw_entry, dict):
            continue
        requests = raw_entry.get("requests")
        if not isinstance(requests, dict) or request_key not in requests:
            continue
        month_request = requests.pop(request_key)
        used = raw_entry.get("used")
        raw_entry["used"] = max(0, (used if isinstance(used, int) else 0) - get_copilot_request_entry_cost(month_request, cost))
        removed_from_month = True

    if not removed_from_month and history_entry is not None:
        month_entry["requests"].pop(request_key, None)
        month_entry["used"] = max(0, month_entry["used"] - get_copilot_request_entry_cost(history_entry, cost))


def rollback_copilot_review_budget_request(
    *,
    budget_file: str,
    month: str,
    request_key: str,
    default_cost: int,
    log_prefix: str,
    reason: str,
) -> int | None:
    try:
        with locked_copilot_review_budget_state(budget_file):
            state = load_copilot_review_budget_state(budget_file)
            month_entry = get_copilot_month_entry(state, month)
            history_entry = get_copilot_request_history(state).get(request_key)
            month_entry_request = month_entry["requests"].get(request_key)
            entry_cost = get_copilot_request_entry_cost(history_entry or month_entry_request, default_cost)
            remove_copilot_review_budget_request(
                state=state,
                month_entry=month_entry,
                request_key=request_key,
                cost=entry_cost,
            )
            save_copilot_review_budget_state(budget_file, state)
            return month_entry["used"]
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        log_progress(log_prefix, f"Copilot budget rollback failed after {reason}: {exc}")
        return None


def mark_copilot_review_budget_request_confirmed(
    *,
    budget_file: str,
    month: str,
    request_key: str,
    log_prefix: str,
) -> int | None:
    try:
        with locked_copilot_review_budget_state(budget_file):
            state = load_copilot_review_budget_state(budget_file)
            month_entry = get_copilot_month_entry(state, month)
            request_history = get_copilot_request_history(state)
            confirmed_at = current_utc_timestamp()

            for entry in (request_history.get(request_key), month_entry["requests"].get(request_key)):
                if isinstance(entry, dict):
                    entry["status"] = "requested"
                    entry["confirmed_at"] = confirmed_at

            save_copilot_review_budget_state(budget_file, state)
            return month_entry["used"]
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        log_progress(log_prefix, f"Copilot review was requested but final budget state could not be saved: {exc}")
        return None


def is_copilot_requested_reviewer(raw_reviewer: dict[str, Any], reviewer: str) -> bool:
    login = normalize_text(raw_reviewer.get("login")).lower()
    normalized_reviewer = reviewer.lower()
    return bool(login) and (login == normalized_reviewer or "copilot" in login)


def build_copilot_review_request_result(
    *,
    status: str,
    reviewer: str,
    reason: str | None = None,
    budget: int | None = None,
    used: int | None = None,
    cost: int | None = None,
    budget_file: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "reviewer": reviewer}
    if reason:
        result["reason"] = reason
    if budget is not None:
        result["monthly_budget"] = budget
    if used is not None:
        result["used"] = used
    if cost is not None:
        result["request_cost"] = cost
    if budget_file:
        result["budget_file"] = budget_file
    return result


def maybe_request_copilot_review(
    github: GitHubApi,
    pull_number: int,
    *,
    existing_review_context: list[dict[str, Any]] | None = None,
    log_prefix: str = "",
) -> dict[str, Any]:
    reviewer = normalize_copilot_reviewer()
    if not env_flag_enabled(COPILOT_REVIEW_REQUEST_ENV):
        return build_copilot_review_request_result(status="disabled", reviewer=reviewer)

    context = existing_review_context or []
    if any(is_copilot_review_context_item(item) for item in context):
        return build_copilot_review_request_result(
            status="skipped",
            reviewer=reviewer,
            reason="copilot_context_already_exists",
        )

    budget = parse_positive_int_env(COPILOT_REVIEW_MONTHLY_BUDGET_ENV, DEFAULT_COPILOT_REVIEW_MONTHLY_BUDGET)
    cost = parse_positive_int_env(COPILOT_REVIEW_REQUEST_COST_ENV, DEFAULT_COPILOT_REVIEW_REQUEST_COST)
    pending_ttl_seconds = parse_positive_int_env(
        COPILOT_REVIEW_PENDING_TTL_SECONDS_ENV,
        DEFAULT_COPILOT_REVIEW_PENDING_TTL_SECONDS,
    )
    api_timeout_seconds = parse_positive_int_env(
        COPILOT_REVIEW_API_TIMEOUT_SECONDS_ENV,
        DEFAULT_COPILOT_REVIEW_API_TIMEOUT_SECONDS,
    )
    budget_file = default_copilot_review_budget_file()
    month = current_copilot_review_budget_month()
    request_key = f"{github.repository}#{pull_number}"

    try:
        with locked_copilot_review_budget_state(budget_file):
            state = load_copilot_review_budget_state(budget_file)
            month_entry = get_copilot_month_entry(state, month)
            request_history = get_copilot_request_history(state)

            existing_entry = request_history.get(request_key) or month_entry["requests"].get(request_key)
            if get_copilot_request_status(existing_entry) == "pending":
                if is_recent_copilot_pending_request(existing_entry, ttl_seconds=pending_ttl_seconds):
                    return build_copilot_review_request_result(
                        status="skipped",
                        reviewer=reviewer,
                        reason="request_pending",
                        budget=budget,
                        used=month_entry["used"],
                        cost=cost,
                        budget_file=budget_file,
                    )

                remove_copilot_review_budget_request(
                    state=state,
                    month_entry=month_entry,
                    request_key=request_key,
                    cost=get_copilot_request_entry_cost(existing_entry, cost),
                )
                save_copilot_review_budget_state(budget_file, state)
            elif request_key in request_history or request_key in month_entry["requests"]:
                return build_copilot_review_request_result(
                    status="skipped",
                    reviewer=reviewer,
                    reason="already_requested_by_budget_state",
                    budget=budget,
                    used=month_entry["used"],
                    cost=cost,
                    budget_file=budget_file,
                )

            if month_entry["used"] + cost > budget:
                log_progress(
                    log_prefix,
                    f"Skipping Copilot review request because budget would be exceeded "
                    f"({month_entry['used']}+{cost}>{budget})",
                )
                return build_copilot_review_request_result(
                    status="skipped",
                    reviewer=reviewer,
                    reason="monthly_budget_exhausted",
                    budget=budget,
                    used=month_entry["used"],
                    cost=cost,
                    budget_file=budget_file,
                )

            record_copilot_review_budget_request(
                state=state,
                month_entry=month_entry,
                request_key=request_key,
                cost=cost,
                reviewer=reviewer,
                month=month,
                status="pending",
            )
            save_copilot_review_budget_state(budget_file, state)
            reserved_used = month_entry["used"]
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        log_progress(log_prefix, f"Skipping Copilot review request because budget state is unavailable: {exc}")
        return build_copilot_review_request_result(
            status="skipped",
            reviewer=reviewer,
            reason="budget_state_unavailable",
            budget=budget,
            cost=cost,
            budget_file=budget_file,
        )

    try:
        requested_reviewers = github.list_requested_reviewers(pull_number, timeout=api_timeout_seconds)
    except GitHubApiError as exc:
        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="GitHub requested-reviewers lookup rejection",
        )
        log_progress(log_prefix, f"Copilot requested-reviewers lookup was rejected by GitHub: {exc.status} {exc.response_body}")
        return build_copilot_review_request_result(
            status="failed",
            reviewer=reviewer,
            reason=f"github_api_{exc.status}",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )
    except (AttributeError, OSError, RuntimeError) as exc:
        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="requested-reviewers lookup error",
        )
        log_progress(log_prefix, f"Copilot requested-reviewers lookup failed; continuing with MLX review: {exc}")
        return build_copilot_review_request_result(
            status="failed",
            reviewer=reviewer,
            reason="request_failed",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )

    if any(is_copilot_requested_reviewer(raw_reviewer, reviewer) for raw_reviewer in requested_reviewers):
        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="existing GitHub reviewer",
        )
        return build_copilot_review_request_result(
            status="skipped",
            reviewer=reviewer,
            reason="already_requested_on_github",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )

    try:
        github.request_reviewers(pull_number, [reviewer], timeout=api_timeout_seconds)
    except GitHubApiError as exc:
        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="GitHub rejection",
        )
        log_progress(log_prefix, f"Copilot review request was rejected by GitHub: {exc.status} {exc.response_body}")
        return build_copilot_review_request_result(
            status="failed",
            reviewer=reviewer,
            reason=f"github_api_{exc.status}",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )
    except AttributeError as exc:
        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="request error",
        )
        log_progress(log_prefix, f"Copilot review request failed; continuing with MLX review: {exc}")
        return build_copilot_review_request_result(
            status="failed",
            reviewer=reviewer,
            reason="request_failed",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )
    except (OSError, RuntimeError) as exc:
        try:
            requested_reviewers = github.list_requested_reviewers(pull_number, timeout=api_timeout_seconds)
        except (GitHubApiError, AttributeError, OSError, RuntimeError) as confirm_exc:
            log_progress(
                log_prefix,
                "Copilot review request outcome is unknown after request error; "
                f"leaving pending budget state: {exc}; confirmation failed: {confirm_exc}",
            )
            return build_copilot_review_request_result(
                status="pending",
                reviewer=reviewer,
                reason="request_outcome_unknown",
                budget=budget,
                used=reserved_used,
                cost=cost,
                budget_file=budget_file,
            )

        if any(is_copilot_requested_reviewer(raw_reviewer, reviewer) for raw_reviewer in requested_reviewers):
            confirmed_used = mark_copilot_review_budget_request_confirmed(
                budget_file=budget_file,
                month=month,
                request_key=request_key,
                log_prefix=log_prefix,
            )
            if confirmed_used is None:
                return build_copilot_review_request_result(
                    status="requested_budget_record_failed",
                    reviewer=reviewer,
                    reason="budget_state_save_failed",
                    budget=budget,
                    used=reserved_used,
                    cost=cost,
                    budget_file=budget_file,
                )
            log_progress(
                log_prefix,
                f"Confirmed Copilot review request after request error; local monthly budget usage is {confirmed_used}/{budget}",
            )
            return build_copilot_review_request_result(
                status="requested",
                reviewer=reviewer,
                reason="confirmed_after_request_error",
                budget=budget,
                used=confirmed_used,
                cost=cost,
                budget_file=budget_file,
            )

        used = rollback_copilot_review_budget_request(
            budget_file=budget_file,
            month=month,
            request_key=request_key,
            default_cost=cost,
            log_prefix=log_prefix,
            reason="request error without GitHub reviewer",
        )
        log_progress(log_prefix, f"Copilot review request failed; continuing with MLX review: {exc}")
        return build_copilot_review_request_result(
            status="failed",
            reviewer=reviewer,
            reason="request_failed",
            budget=budget,
            used=used,
            cost=cost,
            budget_file=budget_file,
        )

    confirmed_used = mark_copilot_review_budget_request_confirmed(
        budget_file=budget_file,
        month=month,
        request_key=request_key,
        log_prefix=log_prefix,
    )
    if confirmed_used is None:
        return build_copilot_review_request_result(
            status="requested_budget_record_failed",
            reviewer=reviewer,
            reason="budget_state_save_failed",
            budget=budget,
            used=reserved_used,
            cost=cost,
            budget_file=budget_file,
        )

    log_progress(
        log_prefix,
        f"Requested Copilot review from @{reviewer}; local monthly budget usage is {confirmed_used}/{budget}",
    )
    return build_copilot_review_request_result(
        status="requested",
        reviewer=reviewer,
        budget=budget,
        used=confirmed_used,
        cost=cost,
        budget_file=budget_file,
    )


def truncate_copilot_review_section_body(value: Any) -> str:
    normalized = normalize_text(value)
    if len(normalized) <= MAX_COPILOT_REVIEW_SECTION_BODY_CHARS:
        return normalized
    return normalized[: MAX_COPILOT_REVIEW_SECTION_BODY_CHARS - 3].rstrip() + "..."


def format_copilot_review_context_item(item: dict[str, Any]) -> str:
    body = truncate_copilot_review_section_body(item.get("body"))
    path = normalize_text(item.get("path"))
    line = coerce_optional_int(item.get("line"))
    if path and line is not None:
        return f"- `{path}:{line}` {body}"
    return f"- {body}"


def build_copilot_review_section(existing_review_context: list[dict[str, Any]] | None) -> list[str]:
    context = existing_review_context or []
    copilot_items = [item for item in context if is_copilot_review_context_item(item)]

    if not copilot_items:
        return []

    body_lines = ["", "## Copilot 리뷰"]
    body_lines.append(f"- 상태: 기존 Copilot 리뷰 코멘트 {len(copilot_items)}건을 확인했습니다.")
    body_lines.append("- 참고: Copilot이 직접 남긴 라인 코멘트는 중복 게시하지 않고 아래에 요약만 표시합니다.")
    recent_copilot_items = copilot_items[-MAX_COPILOT_REVIEW_SECTION_ITEMS:]
    body_lines.extend(
        format_copilot_review_context_item(item)
        for item in reversed(recent_copilot_items)
    )
    if len(copilot_items) > MAX_COPILOT_REVIEW_SECTION_ITEMS:
        body_lines.append(f"- 그 외 {len(copilot_items) - MAX_COPILOT_REVIEW_SECTION_ITEMS}건은 PR 대화에서 확인하세요.")
    return body_lines


def load_existing_review_context(
    github: GitHubApi,
    pull_number: int,
    *,
    log_prefix: str = "",
) -> list[dict[str, Any]]:
    """PR에 이미 달린 댓글과 리뷰 대댓글을 모델 참고용으로 정규화한다."""
    discussion_items: list[PullRequestDiscussionItem] = []

    try:
        raw_review_comments = github.list_review_comments(pull_number)
    except (RuntimeError, OSError) as exc:
        log_progress(log_prefix, f"Skipping existing review comments context: {exc}")
        raw_review_comments = []

    for raw_comment in raw_review_comments:
        if not isinstance(raw_comment, dict):
            continue
        item = build_review_comment_context(raw_comment)
        if item is not None:
            discussion_items.append(item)

    try:
        raw_issue_comments = github.list_issue_comments(pull_number)
    except (RuntimeError, OSError) as exc:
        log_progress(log_prefix, f"Skipping existing issue comments context: {exc}")
        raw_issue_comments = []

    for raw_comment in raw_issue_comments:
        if not isinstance(raw_comment, dict):
            continue
        item = build_issue_comment_context(raw_comment)
        if item is not None:
            discussion_items.append(item)

    discussion_items.sort(
        key=lambda item: (
            item.created_at,
            item.source,
            item.path or "",
            item.line or 0,
            item.comment_id or 0,
        )
    )
    discussion_items = discussion_items[-MAX_EXISTING_REVIEW_CONTEXT_ITEMS:]
    log_progress(log_prefix, f"Loaded {len(discussion_items)} existing PR discussion item(s)")
    return [item.to_prompt_dict() for item in discussion_items]


def parse_right_side_lines(patch: str) -> set[int]:
    """GitHub unified diff에서 리뷰 가능한 RIGHT-side 줄 번호를 추린다."""
    lines: set[int] = set()
    current_new_line = None

    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            parts = raw_line.split()
            new_range = next(part for part in parts if part.startswith("+"))
            start_and_len = new_range[1:]
            if "," in start_and_len:
                start_str, _ = start_and_len.split(",", 1)
            else:
                start_str = start_and_len
            current_new_line = int(start_str)
            continue

        if current_new_line is None:
            continue

        if raw_line.startswith("+"):
            lines.add(current_new_line)
            current_new_line += 1
        elif raw_line.startswith(" "):
            lines.add(current_new_line)
            current_new_line += 1
        elif raw_line.startswith("-"):
            continue
        else:
            current_new_line = None

    return lines


def parse_patch_new_ranges(patch: str) -> list[tuple[int, int]]:
    """unified diff hunk header에서 PR HEAD 기준 줄 범위를 읽는다."""
    ranges: list[tuple[int, int]] = []
    for raw_line in patch.splitlines():
        match = HUNK_HEADER_RE.match(raw_line)
        if match is None:
            continue
        start = int(match.group("start"))
        length = int(match.group("length") or "1")
        ranges.append((start, max(length, 1)))
    return ranges


def merge_line_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """서로 겹치거나 맞닿은 1-based line range를 합친다."""
    if not ranges:
        return []

    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        previous_start, previous_end = merged[-1]
        merged[-1] = (previous_start, max(previous_end, end))
    return merged


def parse_non_negative_int_env(name: str, default: int) -> int:
    raw_value = normalize_text(os.environ.get(name))
    if not raw_value:
        return default
    try:
        parsed = int(raw_value)
    except ValueError:
        return default
    return max(parsed, 0)


def configured_current_file_context_line_radius() -> int:
    return parse_non_negative_int_env(
        CURRENT_FILE_CONTEXT_LINE_RADIUS_ENV,
        DEFAULT_CURRENT_FILE_CONTEXT_LINE_RADIUS,
    )


def configured_current_file_context_max_chars() -> int:
    return parse_non_negative_int_env(
        CURRENT_FILE_CONTEXT_MAX_CHARS_ENV,
        DEFAULT_CURRENT_FILE_CONTEXT_MAX_CHARS,
    )


def configured_current_file_context_mode() -> str:
    mode = normalize_text(os.environ.get(CURRENT_FILE_CONTEXT_MODE_ENV)).lower()
    if mode in {"off", "none", "disabled", "0"}:
        return "off"
    if mode in {"full_repo", "full-repo", "repo", "repository"}:
        return "full_repo"
    if mode in {"full", "full_file", "full-file"}:
        return "full"
    if mode == "auto":
        return "auto"
    if mode == "excerpt":
        return "excerpt"
    return DEFAULT_CURRENT_FILE_CONTEXT_MODE


def configured_repository_context_max_files() -> int:
    return parse_non_negative_int_env(
        REPOSITORY_CONTEXT_MAX_FILES_ENV,
        DEFAULT_REPOSITORY_CONTEXT_MAX_FILES,
    )


def configured_repository_context_max_chars() -> int:
    return parse_non_negative_int_env(
        REPOSITORY_CONTEXT_MAX_CHARS_ENV,
        DEFAULT_REPOSITORY_CONTEXT_MAX_CHARS,
    )


def configured_repository_context_file_max_chars() -> int:
    return parse_non_negative_int_env(
        REPOSITORY_CONTEXT_FILE_MAX_CHARS_ENV,
        DEFAULT_REPOSITORY_CONTEXT_FILE_MAX_CHARS,
    )


def configured_review_context_api_timeout_seconds() -> int:
    return parse_positive_int_env(
        REVIEW_CONTEXT_API_TIMEOUT_SECONDS_ENV,
        DEFAULT_REVIEW_CONTEXT_API_TIMEOUT_SECONDS,
    )


def configured_review_prompt_max_chars() -> int:
    return parse_non_negative_int_env(
        REVIEW_PROMPT_MAX_CHARS_ENV,
        DEFAULT_REVIEW_PROMPT_MAX_CHARS,
    )


def configured_review_context_settings() -> ReviewContextSettings:
    return ReviewContextSettings(
        mode=configured_current_file_context_mode(),
        line_radius=configured_current_file_context_line_radius(),
        max_chars=configured_current_file_context_max_chars(),
        repository_max_files=configured_repository_context_max_files(),
        repository_max_chars=configured_repository_context_max_chars(),
        repository_file_max_chars=configured_repository_context_file_max_chars(),
        api_timeout_seconds=configured_review_context_api_timeout_seconds(),
    )


def truncate_context(text: str, max_chars: int, *, suffix: str) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    suffix_text = f"\n... [{suffix}]"
    if len(suffix_text) >= max_chars:
        return text[:max_chars]
    return text[: max_chars - len(suffix_text)].rstrip() + suffix_text


def build_line_numbered_file_context(file_text: str, *, max_chars: int) -> str:
    """현재 PR HEAD 파일 전체를 line-numbered context로 만든다."""
    context, _truncated = build_line_numbered_file_context_with_truncation(file_text, max_chars=max_chars)
    return context


def build_line_numbered_file_context_with_truncation(file_text: str, *, max_chars: int) -> tuple[str, bool]:
    """현재 PR HEAD 파일 전체 context와 실제 truncation 여부를 함께 반환한다."""
    if max_chars <= 0:
        return "", False
    lines = file_text.splitlines()
    if not lines:
        return "", False
    context = "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(lines, start=1))
    return truncate_context(context, max_chars, suffix="full file context truncated"), len(context) > max_chars


def build_current_file_context_excerpt(
    file_text: str,
    patch: str,
    *,
    line_radius: int,
    max_chars: int,
) -> str:
    """변경 hunk 주변의 현재 PR HEAD 파일 컨텍스트를 line-numbered excerpt로 만든다."""
    if line_radius < 0 or max_chars <= 0:
        return ""

    lines = file_text.splitlines()
    if not lines:
        return ""

    hunk_ranges = parse_patch_new_ranges(patch)
    if not hunk_ranges:
        return ""

    total_lines = len(lines)

    current_radius = max(line_radius, 0)
    last_excerpt = ""
    while True:
        expanded_ranges = []
        for start, length in hunk_ranges:
            end = start + length - 1
            expanded_ranges.append(
                (
                    max(1, start - current_radius),
                    min(total_lines, end + current_radius),
                )
            )

        parts: list[str] = []
        for start, end in merge_line_ranges(expanded_ranges):
            parts.append(f"Lines {start}-{end}:")
            parts.extend(f"{line_number}: {lines[line_number - 1]}" for line_number in range(start, end + 1))

        excerpt = "\n".join(parts)
        if len(excerpt) <= max_chars:
            return excerpt

        last_excerpt = excerpt
        if current_radius == 0:
            return truncate_context(last_excerpt, max_chars, suffix="current context truncated")
        current_radius //= 2


def build_current_file_context(
    file_text: str,
    patch: str,
    *,
    mode: str,
    line_radius: int,
    max_chars: int,
) -> tuple[str, str]:
    """mode별로 최신 PR HEAD 파일 컨텍스트를 만든다."""
    if mode == "off" or max_chars <= 0:
        return "", "off"

    if mode in {"auto", "full", "full_repo"}:
        full_context, full_context_truncated = build_line_numbered_file_context_with_truncation(
            file_text,
            max_chars=max_chars,
        )
        if mode == "full" and full_context:
            context_mode = "full_file_truncated" if full_context_truncated else "full_file"
            return full_context, context_mode
        if full_context and not full_context_truncated:
            return full_context, "full_file"

    excerpt = build_current_file_context_excerpt(
        file_text,
        patch,
        line_radius=line_radius,
        max_chars=max_chars,
    )
    return excerpt, "excerpt" if excerpt else ""


def enrich_pr_files_with_current_context(
    github: GitHubApi,
    pull_number: int,
    files: list[PullRequestFile],
    *,
    settings: ReviewContextSettings,
    log_prefix: str = "",
) -> None:
    """PR HEAD의 현재 파일 excerpt를 붙여 diff 밖 호출 경로도 모델이 검증할 수 있게 한다."""
    if not files:
        return

    if settings.mode == "off":
        log_progress(log_prefix, "Skipping current file context: mode=off")
        return
    if settings.max_chars <= 0:
        log_progress(log_prefix, "Skipping current file context: max_chars<=0")
        return

    try:
        head_sha = github.get_pull_head_sha(pull_number)
    except (RuntimeError, OSError) as exc:
        log_progress(log_prefix, f"Skipping current file context: could not resolve PR head sha: {exc}")
        return

    loaded_count = 0
    outcome_counts: dict[str, int] = {}

    def record_outcome(outcome: str) -> None:
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    for pr_file in files:
        if pr_file.status.lower() in {"removed", "deleted"}:
            record_outcome("skipped_removed")
            continue
        try:
            file_text = github.get_file_text(pr_file.filename, ref=head_sha, timeout=settings.api_timeout_seconds)
        except (RuntimeError, OSError) as exc:
            log_progress(log_prefix, f"Skipping current file context for {pr_file.filename}: {exc}")
            record_outcome("skipped_fetch_error")
            continue

        context, context_mode = build_current_file_context(
            file_text,
            pr_file.patch,
            mode=settings.mode,
            line_radius=settings.line_radius,
            max_chars=settings.max_chars,
        )
        if not context:
            record_outcome("skipped_empty")
            continue
        pr_file.current_file_context = context
        pr_file.current_file_context_mode = context_mode
        record_outcome(context_mode)
        loaded_count += 1

    if outcome_counts:
        outcome_summary = ", ".join(f"{name}={count}" for name, count in sorted(outcome_counts.items()))
        log_progress(
            log_prefix,
            f"Loaded current file context for {loaded_count} patchable file(s) mode={settings.mode} "
            f"outcomes={outcome_summary}",
        )


def repository_context_enabled(mode: str) -> bool:
    return mode == "full_repo"


def tree_item_path(item: dict[str, Any]) -> str:
    return normalize_text(item.get("path"))


def tree_item_size(item: dict[str, Any]) -> int:
    value = item.get("size")
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def repository_context_priority(path: str, changed_paths: set[str]) -> tuple[int, str]:
    dirname = path.rsplit("/", 1)[0] if "/" in path else ""
    changed_dirs = {changed.rsplit("/", 1)[0] if "/" in changed else "" for changed in changed_paths}
    filename = path.rsplit("/", 1)[-1]
    first_segment = path.split("/", 1)[0]

    if dirname and dirname in changed_dirs:
        return (0, path)
    if filename in {"pyproject.toml", "package.json", "Package.swift", "Project.swift", "tsconfig.json"}:
        return (1, path)
    if first_segment in {"review_runner", "src", "app", "lib", "pkg", "internal", "Sources"}:
        return (2, path)
    if first_segment in {"tests", "test"} or filename.startswith("test_"):
        return (3, path)
    return (4, path)


def collect_repository_context(
    github: GitHubApi,
    pull_number: int,
    changed_files: list[PullRequestFile],
    config: ReviewBotConfig,
    *,
    settings: ReviewContextSettings,
    log_prefix: str = "",
) -> list[RepositoryContextEntry]:
    """품질 우선 리뷰를 위해 PR HEAD의 repo 파일 일부를 읽기 전용 컨텍스트로 수집한다."""
    if not repository_context_enabled(settings.mode):
        return []

    max_files = settings.repository_max_files
    max_chars = settings.repository_max_chars
    file_max_chars = settings.repository_file_max_chars
    if max_files <= 0 or max_chars <= 0 or file_max_chars <= 0:
        return []

    changed_paths = {pr_file.filename for pr_file in changed_files}
    try:
        head_sha = github.get_pull_head_sha(pull_number)
        tree = github.list_repo_tree(head_sha, timeout=settings.api_timeout_seconds)
    except (RuntimeError, OSError, AttributeError) as exc:
        log_progress(log_prefix, f"Skipping repository context: {exc}")
        return []

    candidates: list[tuple[tuple[int, str], str, int]] = []
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = tree_item_path(item)
        if not path or path in changed_paths:
            continue
        if not should_review_file(path, config):
            continue
        size = tree_item_size(item)
        if size and size > file_max_chars * 4:
            continue
        candidates.append((repository_context_priority(path, changed_paths), path, size))

    entries: list[RepositoryContextEntry] = []
    total_chars = 0
    for _priority, path, _size in sorted(candidates):
        if len(entries) >= max_files or total_chars >= max_chars:
            break
        minimum_entry_cost = len(path) + 32
        if total_chars + minimum_entry_cost >= max_chars:
            break
        try:
            file_text = github.get_file_text(path, ref=head_sha, timeout=settings.api_timeout_seconds)
        except (RuntimeError, OSError) as exc:
            log_progress(log_prefix, f"Skipping repository context for {path}: {exc}")
            continue

        context, context_truncated = build_line_numbered_file_context_with_truncation(
            file_text,
            max_chars=file_max_chars,
        )
        if not context:
            continue
        entry_cost = len(path) + len(context) + 32
        if total_chars + entry_cost > max_chars:
            continue
        mode_name = "truncated_file" if context_truncated else "full_file"
        entries.append(RepositoryContextEntry(path=path, content=context, mode=mode_name))
        total_chars += entry_cost

    if entries:
        log_progress(
            log_prefix,
            f"Loaded repository context files={len(entries)} chars={total_chars}/{max_chars}",
        )
    return entries


def build_review_focus_hints(files: list[PullRequestFile]) -> list[str]:
    """모델이 놓치기 쉬운 변경 유형별 확인 질문을 payload에 추가한다."""
    combined = "\n".join(f"{pr_file.patch}\n{pr_file.current_file_context}" for pr_file in files).lower()
    if not combined:
        return []

    hints: list[str] = []
    cooldown_markers = (
        "cooldown",
        "rate_limit",
        "rate limit",
        "backoff",
        "retry-after",
        "paused_until",
        "429",
        "server_error",
        "server error",
    )
    touches_cooldown = any(marker in combined for marker in cooldown_markers)
    if touches_cooldown:
        hints.append(
            "이 PR은 provider rate limit/cooldown/backoff 로직을 변경합니다. APPROVE 전에 cooldown 확인과 pause 설정이 제한 대상 API의 모든 현재 호출 경로에 적용되는지, 새 상태가 호출 단위 지역 변수로 리셋되어 반복 실패를 놓치지 않는지 확인하세요."
        )
    if touches_cooldown and "asyncio.gather" in combined:
        hints.append(
            "cooldown 설정과 asyncio.gather/동시 작업이 함께 보이면, 한 작업이 429/5xx를 감지한 뒤 이미 예약된 나머지 작업이 계속 외부 API를 호출하는지 확인하세요."
        )
    if touches_cooldown and re.search(r"\b(?:[a-zA-Z_]\w*)?error_count\s*=\s*0\b", combined):
        hints.append(
            "cooldown 발동 기준이 error_count 같은 지역 변수라면, 한 번의 함수 호출 안에서만 누적되고 다음 호출에서 0으로 초기화되어 단건/소량 반복 실패를 놓치는지 확인하세요."
        )
    if touches_cooldown and "httpx.httpstatuserror" in combined:
        hints.append(
            "httpx.HTTPStatusError로 cooldown 여부를 판단하는 경로는 timeout/connect error 같은 httpx.RequestError 계열이 카운터와 cooldown에서 빠지는지 확인하세요."
        )

    return hints


def build_pr_files(raw_files: list[dict[str, Any]]) -> list[PullRequestFile]:
    """GitHub PR 파일 응답을 내부 리뷰 구조로 변환한다."""
    files: list[PullRequestFile] = []
    for raw in raw_files:
        patch = raw.get("patch") or ""
        if not patch:
            continue
        files.append(
            PullRequestFile(
                filename=raw["filename"],
                status=raw["status"],
                patch=patch,
                additions=int(raw.get("additions", 0)),
                deletions=int(raw.get("deletions", 0)),
                right_side_lines=parse_right_side_lines(patch),
            )
        )
    return files


def strip_reviewbot_yaml_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value[0] in {"'", '"'}:
        quote = value[0]
        escaped = False
        chars: list[str] = []
        for char in value[1:]:
            if escaped:
                chars.append(char)
                escaped = False
                continue
            if quote == '"' and char == "\\":
                escaped = True
                continue
            if char == quote:
                return "".join(chars).strip()
            chars.append(char)
        raise ValueError(f"unterminated {quote} quoted string: {raw_value!r}")
    return value.split(" #", 1)[0].strip()


def parse_reviewbot_config(raw_config: str) -> ReviewBotConfig:
    """리뷰 대상 저장소의 .reviewbot.yml 중 review include/exclude 목록만 읽는다."""
    buckets: dict[str, list[str]] = {
        "include": [],
        "exclude": [],
        "always_review": [],
    }
    in_review_section = False
    review_indent = -1
    current_bucket: str | None = None
    ignored_bucket_indent: int | None = None

    for line_number, raw_line in enumerate(raw_config.splitlines(), start=1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"tabs are not supported in indentation at line {line_number}")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key_line = stripped.split(" #", 1)[0].rstrip()
        if not in_review_section:
            if key_line == "review:":
                in_review_section = True
                review_indent = indent
            continue

        if indent <= review_indent:
            in_review_section = False
            current_bucket = None
            ignored_bucket_indent = None
            if key_line == "review:":
                in_review_section = True
                review_indent = indent
            continue

        if ignored_bucket_indent is not None:
            if indent > ignored_bucket_indent:
                continue
            ignored_bucket_indent = None

        if not stripped.startswith("- ") and ":" in key_line:
            key, raw_value = key_line.split(":", 1)
            key = key.strip()
            value = raw_value.strip()
            if key in buckets and value:
                raise ValueError(f"unsupported value for review.{key} at line {line_number}")
            current_bucket = key if key in buckets else None
            ignored_bucket_indent = None if key in buckets else indent
            continue

        if stripped.startswith("- "):
            if current_bucket is None and ignored_bucket_indent is not None:
                continue
            if current_bucket is None:
                raise ValueError(f"list item outside include/exclude/always_review at line {line_number}")
            try:
                value = strip_reviewbot_yaml_value(stripped[2:])
            except ValueError as exc:
                raise ValueError(f"{exc} at line {line_number}") from exc
            if value:
                buckets[current_bucket].append(value)
            continue

        raise ValueError(f"unsupported review config line at line {line_number}")

    return ReviewBotConfig(
        include=tuple(buckets["include"]),
        exclude=tuple(buckets["exclude"]),
        always_review=tuple(buckets["always_review"]),
        loaded=True,
    )


def is_github_not_found_error(exc: RuntimeError) -> bool:
    if isinstance(exc, GitHubApiError):
        return exc.status == 404
    return bool(re.search(r"\bfailed:\s*404\b", str(exc)))


def default_reviewbot_config() -> ReviewBotConfig:
    return ReviewBotConfig(exclude=DEFAULT_REVIEWBOT_EXCLUDE_PATTERNS)


def load_reviewbot_config(github: GitHubApi, pull_number: int, *, log_prefix: str = "") -> ReviewBotConfig:
    try:
        head_sha = github.get_pull_head_sha(pull_number)
        raw_config = github.get_file_text(REVIEWBOT_CONFIG_PATH, ref=head_sha)
    except RuntimeError as exc:
        if is_github_not_found_error(exc):
            log_progress(
                log_prefix,
                f"No {REVIEWBOT_CONFIG_PATH} found at PR HEAD; applying built-in generated-file excludes",
            )
        else:
            log_progress(
                log_prefix,
                f"Could not load {REVIEWBOT_CONFIG_PATH}; applying built-in generated-file excludes: {exc}",
            )
        return default_reviewbot_config()

    try:
        config = parse_reviewbot_config(raw_config)
    except ValueError as exc:
        log_progress(
            log_prefix,
            f"Ignoring invalid {REVIEWBOT_CONFIG_PATH}; applying built-in generated-file excludes: {exc}",
        )
        return default_reviewbot_config()

    log_progress(
        log_prefix,
        f"Loaded {REVIEWBOT_CONFIG_PATH} "
        f"include={len(config.include)} exclude={len(config.exclude)} always_review={len(config.always_review)}",
    )
    return config


def normalize_review_path(path: str) -> str:
    normalized = path.replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def split_review_path(path: str) -> list[str]:
    return [segment for segment in normalize_review_path(path).split("/") if segment]


def reviewbot_glob_segments_match(pattern_segments: list[str], path_segments: list[str]) -> bool:
    memo: dict[tuple[int, int], bool] = {}

    def matches(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]

        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        else:
            head = pattern_segments[pattern_index]
            if head == "**":
                result = matches(pattern_index + 1, path_index) or (
                    path_index < len(path_segments) and matches(pattern_index, path_index + 1)
                )
            elif path_index == len(path_segments):
                result = False
            elif not fnmatch.fnmatchcase(path_segments[path_index], head):
                result = False
            else:
                result = matches(pattern_index + 1, path_index + 1)

        memo[key] = result
        return result

    return matches(0, 0)


def reviewbot_glob_matches(pattern: str, path: str) -> bool:
    return reviewbot_glob_segments_match(split_review_path(pattern), split_review_path(path))


def matches_any_reviewbot_pattern(path: str, patterns: tuple[str, ...]) -> bool:
    return any(reviewbot_glob_matches(pattern, path) for pattern in patterns)


def should_review_file(path: str, config: ReviewBotConfig) -> bool:
    if matches_any_reviewbot_pattern(path, FORCED_ALWAYS_REVIEW_PATTERNS):
        return True
    if not config.has_filters:
        return True
    if matches_any_reviewbot_pattern(path, config.always_review):
        return True
    if config.include and not matches_any_reviewbot_pattern(path, config.include):
        return False
    return not matches_any_reviewbot_pattern(path, config.exclude)


def filter_reviewbot_files(
    files: list[PullRequestFile],
    config: ReviewBotConfig,
) -> tuple[list[PullRequestFile], int]:
    if not config.has_filters:
        return files, 0
    filtered = [pr_file for pr_file in files if should_review_file(pr_file.filename, config)]
    return filtered, len(files) - len(filtered)


def summarize_comment_bodies(comments: list[ReviewComment], max_items: int = 3) -> list[str]:
    summaries: list[str] = []
    seen: set[str] = set()

    for comment in comments:
        first_line = extract_finding_problem(comment.body) or (comment.body.strip().splitlines()[0] if comment.body.strip() else "")
        text = normalize_text(first_line)
        if not text:
            continue
        if len(text) > 120:
            text = f"{text[:117].rstrip()}..."
        if text in seen:
            continue
        seen.add(text)
        summaries.append(text)
        if len(summaries) >= max_items:
            break

    return summaries


def merge_distinct_items(primary: list[str], secondary: list[str], max_items: int = 5) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    for item in [*primary, *secondary]:
        text = normalize_text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        merged.append(text)
        if len(merged) >= max_items:
            break

    return merged


def iter_patch_lines(patch: str) -> list[tuple[str, int, str]]:
    """패치를 줄 단위로 펼쳐서 종류, 새 파일 줄 번호, 본문을 함께 넘긴다."""
    rows: list[tuple[str, int, str]] = []
    current_new_line: int | None = None

    for raw_line in patch.splitlines():
        if raw_line.startswith("@@"):
            parts = raw_line.split()
            new_range = next(part for part in parts if part.startswith("+"))
            start_and_len = new_range[1:]
            start_str = start_and_len.split(",", 1)[0]
            current_new_line = int(start_str)
            continue

        if current_new_line is None:
            continue

        if raw_line.startswith("+"):
            rows.append(("add", current_new_line, raw_line[1:]))
            current_new_line += 1
        elif raw_line.startswith(" "):
            rows.append(("context", current_new_line, raw_line[1:]))
            current_new_line += 1
        elif raw_line.startswith("-"):
            rows.append(("remove", current_new_line, raw_line[1:]))
        else:
            current_new_line = None

    return rows


def is_test_file_path(path: str) -> bool:
    """규칙 기반 운영 위험 탐지에서 제외할 테스트 파일인지 판정한다."""
    normalized = path.replace("\\", "/")
    filename = normalized.rsplit("/", 1)[-1]
    return normalized.startswith("tests/") or filename.startswith("test_") or filename.endswith("_test.py")


def detect_signature_bypass(pr_file: PullRequestFile) -> list[ReviewComment]:
    findings: list[ReviewComment] = []
    previous_visible_line = ""

    for kind, line_number, text in iter_patch_lines(pr_file.patch):
        stripped = text.strip()
        if kind in {"add", "context"}:
            if kind == "add":
                previous_visible_line_lower = previous_visible_line.lower()
                if stripped == "return" and (
                    "if not signature" in previous_visible_line_lower
                    or "if not hmac.compare_digest" in previous_visible_line_lower
                ):
                    findings.append(
                        ReviewComment(
                            path=pr_file.filename,
                            line=line_number,
                            body=format_finding_body(
                                problem="서명 헤더가 없을 때 바로 반환해 서명 검증이 건너뛰어집니다.",
                                why_it_matters="서명 없는 위조 웹훅이 처리될 수 있습니다.",
                                suggested_fix="누락된 서명은 401로 거부하도록 유지하세요.",
                            ),
                            severity=SEVERITY_CRITICAL,
                        )
                    )
                elif re.search(r"if\s+not\s+.*signature.*:\s*return\b", stripped, re.IGNORECASE) or re.search(
                    r"if\s+not\s+hmac\.compare_digest\(.*\)\s*:\s*return\b",
                    stripped,
                    re.IGNORECASE,
                ):
                    findings.append(
                        ReviewComment(
                            path=pr_file.filename,
                            line=line_number,
                            body=format_finding_body(
                                problem="서명 값이 없거나 불일치해도 return으로 요청을 통과시킵니다.",
                                why_it_matters="인증되지 않은 웹훅을 받아들이게 됩니다.",
                                suggested_fix="서명 누락이나 불일치는 예외를 발생시켜 요청을 거부하세요.",
                            ),
                            severity=SEVERITY_CRITICAL,
                        )
                    )
            previous_visible_line = stripped

    return findings


def detect_secret_logging(pr_file: PullRequestFile) -> list[ReviewComment]:
    first_match_line: int | None = None
    match_count = 0

    for kind, line_number, text in iter_patch_lines(pr_file.patch):
        if kind != "add":
            continue

        if LOG_CALL_RE.search(text) and SECRET_LOG_RE.search(text):
            match_count += 1
            if first_match_line is None:
                first_match_line = line_number

    if first_match_line is None:
        return []

    if match_count == 1:
        problem = "토큰이나 secret 값을 로그에 남깁니다."
    else:
        problem = "이 파일에서 토큰이나 secret 값을 로그에 남기는 코드가 여러 곳 추가되었습니다."

    return [
        ReviewComment(
            path=pr_file.filename,
            line=first_match_line,
            body=format_finding_body(
                problem=problem,
                why_it_matters="서버 로그 접근만으로 인증 정보가 유출될 수 있습니다.",
                suggested_fix="민감한 값은 출력하지 말고, 필요하면 마스킹된 메타데이터만 기록하세요.",
            ),
            severity=SEVERITY_CRITICAL,
        )
    ]


def detect_contract_typos(pr_file: PullRequestFile) -> list[ReviewComment]:
    findings: list[ReviewComment] = []

    for kind, line_number, text in iter_patch_lines(pr_file.patch):
        if kind != "add":
            continue

        for typo_parts, expected in COMMON_TYPO_FIXES.items():
            typo = "".join(typo_parts)
            if f'"{typo}"' not in text and f"'{typo}'" not in text:
                continue
            findings.append(
                ReviewComment(
                    path=pr_file.filename,
                    line=line_number,
                    body=format_finding_body(
                        problem=f"`{typo}` 오타 때문에 기존 계약에서 기대하는 `{expected}` 키나 헤더를 찾지 못합니다.",
                        why_it_matters="공개 응답 필드나 GitHub 헤더 이름이 어긋나 호출 흐름이 깨질 수 있습니다.",
                        suggested_fix=f"공개 응답 필드와 GitHub 헤더 이름은 `{expected}`로 정확히 유지하세요.",
                    ),
                    severity=SEVERITY_MAJOR,
                )
            )
            break

    return findings


def detect_rule_based_comments(files: list[PullRequestFile]) -> list[ReviewComment]:
    """모델이 놓치기 쉬운 보안/계약 위반 패턴을 규칙 기반으로 보강한다."""
    comments: list[ReviewComment] = []
    seen: set[tuple[str, int, str]] = set()
    detectors = (
        detect_signature_bypass,
        detect_secret_logging,
        detect_contract_typos,
    )

    for pr_file in files:
        if is_test_file_path(pr_file.filename):
            continue
        for detector in detectors:
            for comment in detector(pr_file):
                key = (comment.path, comment.line, comment.body)
                if key in seen:
                    continue
                seen.add(key)
                comments.append(comment)

    return comments


def strip_top_level_finding_anchor(text: str, path: str, line: int) -> str:
    """``path:line`` 으로 시작하는 top-level finding 에서 라인 anchor 를 제거한다."""
    prefix_re = re.compile(
        r"^\s*(?:[-*]\s*)?(?:\[(?:Blocking|Critical|Major|Minor|Suggestion)\]\s*)?"
        + re.escape(path)
        + r":"
        + str(line)
        + r"\s*(?:[-–—|:]\s*)?(?:\[(?:Blocking|Critical|Major|Minor|Suggestion)\]\s*)?"
        + r"(?:[-–—|:]\s*)?",
        re.IGNORECASE,
    )
    return normalize_text(prefix_re.sub("", text, count=1))


def extract_top_level_finding_anchor(
    text: str,
    file_index: dict[str, PullRequestFile],
) -> tuple[PullRequestFile, int] | None:
    """top-level finding 텍스트에서 현재 PR의 정확한 ``path:line`` anchor 를 찾는다."""
    normalized = normalize_text(text)
    for path, pr_file in sorted(file_index.items(), key=lambda item: len(item[0]), reverse=True):
        match = re.search(rf"(?<![\w./-]){re.escape(path)}:(\d+)(?!\d)", normalized)
        if match is None:
            continue
        line = int(match.group(1))
        return pr_file, line
    return None


def extract_explicit_top_level_finding_severity(text: str) -> str | None:
    """top-level finding 텍스트에 명시된 severity 라벨만 읽는다."""
    normalized = normalize_text(text)
    bracket_match = re.search(r"\[(Blocking|Critical|Major|Minor|Suggestion)\]", normalized, re.IGNORECASE)
    if bracket_match is not None:
        return normalize_severity(bracket_match.group(1))
    field_match = re.search(
        r"(?:severity|심각도)\s*[:=]\s*(Blocking|Critical|Major|Minor|Suggestion)",
        normalized,
        re.IGNORECASE,
    )
    if field_match is not None:
        return normalize_severity(field_match.group(1))
    return None


def has_blocking_concern_marker(body: str) -> bool:
    """legacy concern 본문의 Problem 섹션이 차단성 위험 신호를 담고 있는지 확인한다."""
    problem = extract_finding_problem(body)
    return any(marker in problem for marker in CONCERN_BLOCKING_PROMOTION_MARKERS)


def extract_top_level_finding_severity(
    text: str,
    default: str,
    *,
    field_name: str | None = None,
    body: str = "",
) -> str:
    """top-level finding severity 를 보수적으로 결정한다.

    legacy concerns 계열은 구 스키마라 차단성 여부를 명확히 전달하지 못한다. 따라서
    명시 severity 가 없으면 기본 Minor 로 두고, Problem 섹션에 강한 위험 신호가
    있을 때만 Major 로 올린다.
    """
    explicit = extract_explicit_top_level_finding_severity(text)
    if explicit is not None:
        return explicit
    if field_name in LINE_ANCHORED_LEGACY_CONCERN_FIELDS and has_blocking_concern_marker(body):
        return SEVERITY_MAJOR
    return default


def collect_line_anchored_top_level_findings(
    result: dict[str, Any],
    files: list[PullRequestFile],
    seen_comment_keys: set[tuple[str, int, str]],
    stats: CommentValidationStats,
    *,
    max_model_findings: int,
) -> list[ReviewComment]:
    """comments[] 형식에서 벗어난 실제 오류를 좁은 조건으로 라인 코멘트로 복구한다.

    허용 조건은 일부러 빡빡하다: 현재 PR 파일의 정확한 ``path:line`` 이 있어야 하고,
    그 line 이 GitHub RIGHT-side comment line 이어야 하며, 본문은 표준
    Problem/Why/Suggested fix/Confidence 형식을 만족해야 한다. 이 조건을 통과한
    항목만 복구하므로 top-level 자유문장 환각은 계속 버린다.
    """
    file_index = {f.filename: f for f in files}
    recovered: list[ReviewComment] = []
    buckets = (
        ("must_fix", SEVERITY_MAJOR),
        ("suggestions", SEVERITY_MINOR),
        ("legacy_concerns", SEVERITY_MINOR),
        ("concerns", SEVERITY_MINOR),
    )

    for field_name, default_severity in buckets:
        for raw_item in normalize_text_list(result.get(field_name), max_items=10):
            stats.raw_top_level_findings += 1
            anchor = extract_top_level_finding_anchor(raw_item, file_index)
            if anchor is None:
                increment_reason(stats.dropped_top_level_finding_reasons, "missing_line_anchor")
                continue

            pr_file, line = anchor
            if line not in pr_file.right_side_lines:
                increment_reason(stats.dropped_top_level_finding_reasons, "invalid_right_side_line")
                continue

            body = strip_top_level_finding_anchor(raw_item, pr_file.filename, line)
            if not body:
                increment_reason(stats.dropped_top_level_finding_reasons, "empty_body")
                continue
            if looks_like_praise_only_comment(extract_finding_problem(body)):
                increment_reason(stats.dropped_top_level_finding_reasons, "style_or_praise_only")
                continue
            if not has_required_finding_sections(body):
                increment_reason(stats.dropped_top_level_finding_reasons, "missing_required_finding_sections")
                continue

            confidence_label = extract_confidence_label(body)
            if confidence_label is None:
                increment_reason(stats.dropped_top_level_finding_reasons, "invalid_confidence_label")
                continue

            confidence = confidence_score_for_label(confidence_label)
            if confidence is None or confidence < MIN_MODEL_COMMENT_CONFIDENCE:
                increment_reason(stats.dropped_top_level_finding_reasons, "low_confidence")
                continue

            severity = extract_top_level_finding_severity(
                raw_item,
                default_severity,
                field_name=field_name,
                body=body,
            )

            key = (pr_file.filename, line, body)
            if key in seen_comment_keys:
                increment_reason(stats.dropped_top_level_finding_reasons, "duplicate_top_level_finding")
                continue
            if model_finding_limit_reached(stats, max_model_findings):
                increment_reason(stats.dropped_top_level_finding_reasons, "max_findings_exceeded")
                continue

            seen_comment_keys.add(key)
            recovered.append(
                ReviewComment(
                    path=pr_file.filename,
                    line=line,
                    body=body,
                    severity=severity,
                    confidence=confidence,
                )
            )
            stats.accepted_top_level_findings += 1

    return recovered


def is_placeholder_summary(summary: str) -> bool:
    normalized = normalize_text(summary)
    return not normalized or normalized in {
        "Automated review completed.",
        "Automated MLX review completed.",
        "No actionable issues found.",
        "자동 리뷰를 완료했습니다.",
        "자동 MLX 리뷰를 완료했습니다.",
        "검토할 만한 문제를 찾지 못했습니다.",
        "지적할 만한 문제는 보이지 않습니다.",
    }


def looks_like_prompt_echo(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(marker in normalized for marker in PROMPT_ECHO_MARKERS)


def looks_like_diff_stat_dump(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False

    if len(DIFF_STAT_RE.findall(normalized)) >= 4:
        return True

    number_count = len(re.findall(r"\d+", normalized))
    stat_word_count = sum(normalized.count(word) for word in ("추가", "삭제", "변경"))
    return number_count >= 8 and stat_word_count >= 6


def looks_like_generic_positive(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in LOW_SIGNAL_POSITIVE_MARKERS)


def looks_like_generic_model_change_comment(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in LOW_SIGNAL_MODEL_CHANGE_MARKERS)


def looks_like_process_policy_comment(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return False
    return any(marker in normalized for marker in PROCESS_POLICY_MARKERS)


def looks_like_descriptive_change_narration(text: str) -> bool:
    """diff 가 수행한 구조 변경을 사실 서술로만 적은 concern 을 식별한다.

    '~추가되었습니다', '~변경되었습니다' 처럼 서술형 어미로 끝나는 문장은 기본적으로
    concern 이 아니라 변경 요약이다. 다만 같은 문장/문단 안에 '위험', '누락' 같은
    위험 신호가 하나라도 있으면 실제 문제 지적일 수 있으므로 제외한다.
    """
    normalized = normalize_text(text)
    if not normalized:
        return False

    # MLX 출력은 주로 '.' 로 끝나지만 간혹 !/?/~/。 가 섞여 나오니 말미 구두점을 일괄 제거한다.
    stripped = normalized.rstrip(NARRATION_TRAILING_PUNCTUATION)
    # str.endswith 는 suffix 튜플을 그대로 받아 C 레벨에서 매칭하므로 제너레이터보다 간결하고 빠르다.
    if not stripped.endswith(DESCRIPTIVE_NARRATION_SUFFIXES):
        return False

    return not any(marker in normalized for marker in CONCERN_RISK_MARKERS)


def looks_like_positive_only_concern(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return False

    if any(marker in normalized for marker in POSITIVE_CONCERN_MARKERS):
        return True

    if (
        "추가되어" in normalized
        and "할 수 있습니다" in normalized
        and not any(marker in normalized for marker in ("필요", "주의", "위험", "문제", "누락", "부족", "검토"))
    ):
        return True

    return False


def looks_like_identifier_localization_comment(text: str) -> bool:
    normalized = normalize_text(text).lower()
    if not normalized:
        return False

    if "영어로 작성" not in normalized and "영문" not in normalized:
        return False

    if "한국어로 변경" not in normalized and "한글로 변경" not in normalized:
        return False

    # 공개 계약이나 사용자 노출 문자열이 아니라 내부 식별자 이름만 지적하는 경우는 스타일 코멘트로 본다.
    if any(marker in normalized for marker in ("응답", "헤더", "api", "계약", "사용자", "노출", "문구", "메시지")):
        return False

    return True


def looks_like_no_findings_summary(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(marker in normalized for marker in NO_FINDINGS_SUMMARY_MARKERS)


def sanitize_summary(summary: Any, has_findings: bool) -> str:
    normalized = normalize_text(summary)
    fallback = DEFAULT_FINDINGS_SUMMARY if has_findings else DEFAULT_NO_FINDINGS_SUMMARY

    if (
        is_placeholder_summary(normalized)
        or (has_findings and looks_like_no_findings_summary(normalized))
        or looks_like_prompt_echo(normalized)
        or looks_like_diff_stat_dump(normalized)
        or looks_like_generic_model_change_comment(normalized)
    ):
        return fallback

    return normalized


def make_prompt(
    repository: str,
    pull_number: int,
    files: list[PullRequestFile],
    *,
    repository_context: list[RepositoryContextEntry] | None = None,
    existing_review_context: list[dict[str, Any]] | None = None,
) -> str:
    """모델이 바로 읽을 수 있는 JSON 프롬프트를 조립한다."""
    review_focus_hints = build_review_focus_hints(files)
    prompt_payload = {
        "repository": repository,
        "pull_request": pull_number,
        "instructions": {
            "task": "이 PR diff를 리뷰하고, 실제로 수정이 필요한 문제를 구체적으로 알려주세요.",
            "language_rules": [
                "summary, positives, must_fix, suggestions, comments의 모든 문장은 반드시 한국어로 작성하세요.",
                "톤은 전문적이고 간결하게 유지하세요.",
                "칭찬은 positives에만 작성하고, 라인 코멘트에는 작성하지 마세요.",
            ],
            "json_rules": [
                "최상위 키는 summary, event, positives, must_fix, suggestions, comments만 사용하세요.",
                "positives, must_fix, suggestions는 반드시 JSON 배열로 반환하세요.",
                "must_fix와 suggestions에는 finding을 넣지 말고 []를 반환하세요. 모든 finding은 comments에만 작성하세요.",
                "summary 문자열 안에 positive1:, must_fix:, suggestions:, comments: 같은 라벨을 섞어 쓰지 마세요.",
                "event 값은 APPROVE, COMMENT, REQUEST_CHANGES 중 하나만 사용하세요.",
            ],
            "line_comment_rules": [
                "라인 코멘트는 실제 diff에서 보이는 문제만 지적하세요.",
                "반드시 각 파일의 valid_comment_lines 안에 있는 RIGHT-side line 번호만 사용하세요.",
                "정확성, 보안, 안정성, 성능, 변경된 동작에 대한 누락 테스트를 우선하세요.",
                "스타일-only 코멘트나 칭찬-only 코멘트는 금지합니다.",
                "각 코멘트에는 severity, numeric confidence, Problem, Why it matters, Suggested fix, Confidence(High/Medium/Low)를 포함하세요.",
                "최신 PR HEAD의 현재 파일과 line을 기준으로만 지적하고, outdated diff나 이미 수정된 코드는 지적하지 마세요.",
                "guard, early return, optional 여부, 타입 선언, 배열 empty 방어, 상태 전이 조건을 먼저 확인하세요.",
                "Blocking/Major는 재현 가능한 입력, 상태, 실행 순서와 High confidence가 있을 때만 사용하세요.",
                "테스트 지적은 현재 테스트를 확인한 뒤 어떤 실패 모드를 막는지 설명할 수 있을 때만 작성하세요.",
                f"confidence가 {MIN_MODEL_COMMENT_CONFIDENCE:.2f} 미만이면 코멘트를 작성하지 마세요.",
            ],
            "file_context_rules": [
                "patch는 GitHub에 실제 코멘트를 달기 위한 anchor용 diff입니다. current_file_context는 최신 PR HEAD의 변경 파일 전체를 line-numbered 형태로 제공하며, 예산 때문에 잘린 경우 full_file_truncated로 표시됩니다.",
                "auto 또는 excerpt 모드를 명시한 경우에만 큰 파일의 변경 hunk 주변 excerpt가 current_file_context에 들어갈 수 있습니다.",
                "repository_context는 최신 PR HEAD에서 예산 안에 들어온 변경 외 파일들의 읽기 전용 컨텍스트입니다. diff 밖 함수, 기존 호출자, 공용 helper와의 상호작용은 current_file_context와 repository_context로 확인하세요.",
                "문제가 current_file_context의 unchanged line에서 드러나더라도, comments[].line은 반드시 valid_comment_lines 중 이 문제를 새로 만든 changed/context line으로 선택하세요. 적절한 valid line이 없으면 코멘트를 작성하지 마세요.",
            ],
            "existing_review_context_rules": [
                "existing_review_context가 있으면 Copilot, 다른 봇, 사용자 댓글과 대댓글의 최근 논의를 참고하세요.",
                "이미 제기된 지적은 최신 PR HEAD의 diff와 파일 컨텍스트로 다시 증명될 때만 반복하세요.",
                "동일한 path/line의 동일한 문제가 Copilot 리뷰 코멘트에 이미 있으면 comments에 중복 작성하지 마세요.",
                "이미 반박되었거나 해결된 false positive를 다시 코멘트하지 마세요.",
                "다른 리뷰어의 코멘트를 그대로 복사하지 말고, 코드 증거와 재현 가능한 조건이 있을 때만 comments에 작성하세요.",
            ],
            "summary_rules": [
                "summary는 전체 변경을 한두 문장으로 요약하세요.",
                "positives에는 실제 기술적 개선점이 있을 때만 0~2개 작성하세요.",
                "문제가 없더라도 positives를 억지로 채우지 마세요. 중립 summary만으로 충분합니다.",
                "라인 코멘트와 summary 내용은 diff에 근거해야 합니다.",
                "파일별 추가/삭제/변경 개수나 line 번호를 summary에 나열하지 마세요.",
            ],
            "response_schema": {
                "summary": "짧은 전체 리뷰 요약 (한국어)",
                "event": "APPROVE, COMMENT 또는 REQUEST_CHANGES",
                "positives": [
                    "좋은 점 한 항목 (한국어 문자열)",
                ],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "relative/file.py",
                        "line": 12,
                        "severity": "Major",
                        "confidence": 0.92,
                        "body": "Problem: 문제. Why it matters: 영향. Suggested fix: 수정 방법. Confidence: High",
                    }
                ],
            },
        },
        "files": [
            {
                "path": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "valid_comment_lines": sorted(f.right_side_lines),
                "patch": f.patch,
                "current_file_context": f.current_file_context,
                "current_file_context_mode": f.current_file_context_mode,
            }
            for f in files
        ],
    }
    if review_focus_hints:
        prompt_payload["instructions"]["review_focus_hints"] = review_focus_hints
    if repository_context:
        prompt_payload["repository_context"] = [
            {
                "path": entry.path,
                "mode": entry.mode,
                "content": entry.content,
            }
            for entry in repository_context
        ]
    if existing_review_context:
        prompt_payload["existing_review_context"] = existing_review_context
    return json.dumps(prompt_payload, ensure_ascii=False, indent=2)


def write_prompt_debug_file(prompt: str) -> None:
    """문제 재현이 필요할 때 마지막 프롬프트를 파일로 남긴다."""
    debug_path = os.environ.get("PROMPT_DEBUG_PATH", "/tmp/mlx_pr_review_prompt.json")
    with open(debug_path, "w", encoding="utf-8") as fh:
        fh.write(prompt)


def run_mlx_inprocess(prompt: str) -> dict[str, Any]:
    """기본 MLX 클라이언트는 서버 프로세스 안에서 직접 실행해 모델을 재사용한다."""
    from review_runner.mlx_review_client import review_payload

    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MLX prompt payload must be valid JSON") from exc
    return review_payload(payload)


def run_mlx_remote(prompt: str) -> dict[str, Any]:
    """원격 mlx-final-py /v1/generate 를 호출해 같은 모델 인스턴스를 공유한다."""
    from review_runner.mlx_remote_review_client import review_payload

    try:
        payload = json.loads(prompt)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MLX prompt payload must be valid JSON") from exc
    return review_payload(payload)


def configured_mlx_backend() -> str:
    """MLX_REVIEW_BACKEND 가 명시되면 우선, 없으면 MLX_GENERATE_URL 유무로 추정."""
    backend = (os.environ.get("MLX_REVIEW_BACKEND") or "").strip().lower()
    if backend in {"remote", "local"}:
        return backend
    if backend:
        raise RuntimeError("MLX_REVIEW_BACKEND must be one of: local, remote")
    if os.environ.get("MLX_GENERATE_URL"):
        return "remote"
    return "local"


def current_mlx_device_setting() -> str:
    """현재 프로세스에 적용된 MLX 장치 설정을 auto/cpu/gpu 중 하나로 정규화한다."""
    raw_value = os.environ.get("MLX_DEVICE")
    if raw_value is None:
        return "auto"

    device_name = raw_value.strip().lower()
    if device_name in {"", "auto", "default"}:
        return "auto"
    return device_name


def run_mlx_subprocess_attempt(
    command: list[str],
    prompt: str,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """MLX subprocess 한 번을 실행하고 원시 결과를 돌려준다."""
    return subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def describe_mlx_subprocess_failure(completed: subprocess.CompletedProcess[str]) -> str:
    """subprocess 실패를 운영자가 바로 이해할 수 있게 문자열로 포맷한다."""
    failure_reason = f"exit code {completed.returncode}"
    if completed.returncode < 0:
        signal_number = -completed.returncode
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            failure_reason = f"signal {signal_number}"
        else:
            failure_reason = f"signal {signal_number} ({signal_name})"

    native_abort_hint = ""
    if completed.returncode in {-signal.SIGABRT, 128 + signal.SIGABRT}:
        native_abort_hint = (
            "\nHINT:\n"
            "The MLX worker aborted with SIGABRT, which usually means a native MLX/Metal failure rather than a Python exception. "
            "If this repeats on this Mac, try exporting MLX_DEVICE=cpu before starting the server or the MLX worker."
        )

    return (
        "MLX command failed with "
        f"{failure_reason}{native_abort_hint}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
    )


def is_recoverable_mlx_native_failure(completed: subprocess.CompletedProcess[str]) -> bool:
    """Metal/GPU 네이티브 abort처럼 CPU 재시도로 회복될 가능성이 큰 경우만 잡아낸다."""
    if completed.returncode in {-signal.SIGABRT, 128 + signal.SIGABRT}:
        return True

    stderr = completed.stderr.lower()
    native_failure_markers = (
        "[metal]",
        "insufficient memory",
        "kiogpucommandbuffercallbackerroroutofmemory",
        "command buffer execution failed",
        "com.metal.completionqueuedispatch",
        "libmlx.dylib",
    )
    return any(marker in stderr for marker in native_failure_markers)


def parse_mlx_subprocess_output(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    """성공한 subprocess stdout을 검증해 JSON으로 파싱한다."""
    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError("MLX command returned empty output")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MLX command returned invalid JSON:\n{stdout}") from exc


def run_mlx_subprocess(command: list[str], prompt: str, *, log_prefix: str = "") -> dict[str, Any]:
    """커스텀 MLX 어댑터는 기존처럼 subprocess로 실행하되 Metal abort면 CPU로 한 번 재시도한다."""
    completed = run_mlx_subprocess_attempt(command, prompt)
    if completed.returncode != 0:
        if current_mlx_device_setting() == "auto" and is_recoverable_mlx_native_failure(completed):
            retry_env = os.environ.copy()
            retry_env["MLX_DEVICE"] = "cpu"
            log_progress(log_prefix, "MLX worker hit a native Metal failure; retrying once with MLX_DEVICE=cpu")
            retry_completed = run_mlx_subprocess_attempt(command, prompt, env=retry_env)
            if retry_completed.returncode == 0:
                log_progress(log_prefix, "MLX CPU fallback succeeded")
                return parse_mlx_subprocess_output(retry_completed)
            raise RuntimeError(
                "MLX command failed on the default device, and CPU fallback also failed.\n"
                f"INITIAL ATTEMPT:\n{describe_mlx_subprocess_failure(completed)}\n\n"
                f"CPU FALLBACK:\n{describe_mlx_subprocess_failure(retry_completed)}"
            )

        raise RuntimeError(describe_mlx_subprocess_failure(completed))

    return parse_mlx_subprocess_output(completed)


def run_mlx(prompt: str, *, log_prefix: str = "") -> dict[str, Any]:
    """MLX 리뷰 실행은 한 번에 하나씩 처리해 모델 중복 로드와 메모리 급증을 막는다."""
    backend = configured_mlx_backend()
    command = configured_mlx_review_command()
    lock_acquired = _MLX_RUN_LOCK.acquire(blocking=False)
    if not lock_acquired:
        log_progress(log_prefix, "Another MLX review is already running; waiting for the shared model slot")
        _MLX_RUN_LOCK.acquire()

    try:
        if backend == "remote":
            return run_mlx_remote(prompt)
        if uses_inprocess_mlx_client(command):
            return run_mlx_inprocess(prompt)
        return run_mlx_subprocess(command, prompt, log_prefix=log_prefix)
    finally:
        _MLX_RUN_LOCK.release()


def log_mlx_result_metadata(result: dict[str, Any], log_prefix: str) -> None:
    metadata = result.get("_meta")
    if not isinstance(metadata, dict):
        log_progress(log_prefix, "MLX parser metadata unavailable")
        return

    parse_mode = metadata.get("parse_mode", "unknown")
    parse_error = normalize_text(metadata.get("parse_error"))
    raw_comment_count = metadata.get("raw_comment_count", 0)
    normalized_comment_count = metadata.get("normalized_comment_count", 0)
    dropped_reasons = metadata.get("dropped_comment_reasons")
    dropped_text = format_reason_counts(dropped_reasons) if isinstance(dropped_reasons, dict) else "none"

    message = (
        f"MLX parser parse_mode={parse_mode} raw_comments={raw_comment_count} "
        f"normalized_comments={normalized_comment_count} dropped_after_parse={dropped_text}"
    )
    if parse_error:
        message += f" parse_error={parse_error}"
    log_progress(log_prefix, message)


def log_comment_validation_stats(stats: CommentValidationStats, log_prefix: str) -> None:
    log_progress(
        log_prefix,
        "Comment validation "
        f"accepted_model_comments={stats.accepted_model_comments}/{stats.raw_model_comments} "
        f"accepted_top_level_findings={stats.accepted_top_level_findings}/{stats.raw_top_level_findings} "
        f"rule_based_added={stats.rule_based_added} "
        f"rule_based_duplicates={stats.rule_based_duplicates} "
        f"dropped_after_validation={format_reason_counts(stats.dropped_model_comment_reasons)} "
        f"dropped_top_level={format_reason_counts(stats.dropped_top_level_finding_reasons)}",
    )


def configured_max_model_findings() -> int:
    """서비스 검증 단계에서 허용할 모델 finding 총량을 읽는다."""
    raw_value = os.environ.get(MAX_MODEL_FINDINGS_ENV)
    if raw_value is None:
        return DEFAULT_MAX_MODEL_FINDINGS
    try:
        value = int(raw_value)
    except ValueError:
        return DEFAULT_MAX_MODEL_FINDINGS
    return max(1, value)


def model_finding_limit_reached(stats: CommentValidationStats, max_model_findings: int) -> bool:
    """comments[]와 top-level 복구 finding이 공유하는 모델 finding 상한을 확인한다."""
    return stats.accepted_model_comments + stats.accepted_top_level_findings >= max_model_findings


def collect_validated_comments(
    result: dict[str, Any],
    files: list[PullRequestFile],
    *,
    max_model_findings: int | None = None,
) -> tuple[list[ReviewComment], CommentValidationStats]:
    """모델 코멘트와 규칙 기반 코멘트를 합치고 중복을 제거한다."""
    file_index = {f.filename: f for f in files}
    comments: list[ReviewComment] = []
    seen_comment_keys: set[tuple[str, int, str]] = set()
    raw_comments = result.get("comments", [])
    stats = CommentValidationStats(raw_model_comments=len(raw_comments) if isinstance(raw_comments, list) else 0)
    max_model_findings = max_model_findings or configured_max_model_findings()

    for raw in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw, dict):
            increment_reason(stats.dropped_model_comment_reasons, "non_object_comment")
            continue
        path = raw.get("path")
        line = raw.get("line")
        body = normalize_text(raw.get("body"))
        if not path:
            increment_reason(stats.dropped_model_comment_reasons, "missing_path")
            continue
        if not isinstance(line, int) or isinstance(line, bool) or line <= 0:
            increment_reason(stats.dropped_model_comment_reasons, "invalid_line_type")
            continue
        if not body:
            increment_reason(stats.dropped_model_comment_reasons, "empty_body")
            continue
        if looks_like_praise_only_comment(extract_finding_problem(body)):
            increment_reason(stats.dropped_model_comment_reasons, "style_or_praise_only")
            continue
        if not has_required_finding_sections(body):
            increment_reason(stats.dropped_model_comment_reasons, "missing_required_finding_sections")
            continue
        confidence_label = extract_confidence_label(body)
        if confidence_label is None:
            increment_reason(stats.dropped_model_comment_reasons, "invalid_confidence_label")
            continue

        pr_file = file_index.get(path)
        # GitHub Review API는 실제 patch의 RIGHT-side 라인만 허용하므로 여기서 엄격하게 거른다.
        if pr_file is None:
            increment_reason(stats.dropped_model_comment_reasons, "path_mismatch")
            continue
        if line not in pr_file.right_side_lines:
            increment_reason(stats.dropped_model_comment_reasons, "invalid_right_side_line")
            continue

        confidence = normalize_confidence(raw.get("confidence"))
        if confidence is None:
            increment_reason(stats.dropped_model_comment_reasons, "missing_or_invalid_confidence")
            continue
        if confidence < MIN_MODEL_COMMENT_CONFIDENCE:
            increment_reason(stats.dropped_model_comment_reasons, "low_confidence")
            continue

        severity = normalize_severity(raw.get("severity"))
        if severity in BLOCKING_SEVERITIES and confidence_label != "high":
            increment_reason(stats.dropped_model_comment_reasons, "blocking_without_high_confidence")
            continue

        key = (path, line, body)
        if key in seen_comment_keys:
            increment_reason(stats.dropped_model_comment_reasons, "duplicate_model_comment")
            continue
        if model_finding_limit_reached(stats, max_model_findings):
            increment_reason(stats.dropped_model_comment_reasons, "max_findings_exceeded")
            continue
        seen_comment_keys.add(key)
        # 모델이 severity 를 생략하거나 이상한 값을 실어도 Minor 로 폴백해
        # 잘못된 Blocking 승격으로 REQUEST_CHANGES 가 과발동하지 않도록 한다.
        # 패턴 4 (description 재진술의 Major 태그) 는 looks_like_praise_only_comment
        # 가 이미 위에서 코멘트를 drop 하므로 별도 severity 강등 단계는 두지 않는다.
        comments.append(ReviewComment(path=path, line=line, body=body, severity=severity, confidence=confidence))
        stats.accepted_model_comments += 1

    comments.extend(
        collect_line_anchored_top_level_findings(
            result,
            files,
            seen_comment_keys,
            stats,
            max_model_findings=max_model_findings,
        )
    )

    for comment in detect_rule_based_comments(files):
        key = (comment.path, comment.line, comment.body)
        if key in seen_comment_keys:
            stats.rule_based_duplicates += 1
            continue
        seen_comment_keys.add(key)
        comments.append(comment)
        stats.rule_based_added += 1

    return comments, stats


def decide_review_event(
    *,
    should_request_changes: bool,
    has_any_finding: bool,
) -> str:
    """최종 리뷰 event 를 두 플래그만으로 결정한다.

    3 단계 판정:
    - should_request_changes=True → REQUEST_CHANGES. must_fix 또는 Blocking/Major
      라인 코멘트가 있으면 이 분기로 간다.
    - has_any_finding=False → APPROVE. 지적이 하나도 없을 때만. '명시적 승인' 의사.
    - 나머지(suggestions 또는 Minor 라인 코멘트만 있는 경우) → COMMENT.

    모델이 event 를 어떻게 emit 하든 결과는 이 두 플래그로만 결정되므로 raw_event
    인자는 받지 않는다. 필요하다면 호출부에서 로깅 목적으로 모델 원본 값을 따로 보관.
    """
    if should_request_changes:
        return "REQUEST_CHANGES"
    if not has_any_finding:
        return "APPROVE"
    return "COMMENT"


def _tokenize_for_dedup(text: str) -> set[str]:
    """중복 판정용 토큰 집합을 만든다. 구두점·대소문자·공백 차이를 흡수한다.

    Python 3 의 ``re`` 는 기본적으로 ``\\w`` 를 유니코드(한글 포함)로 매칭하므로
    별도로 한글 범위를 추가할 필요가 없고, ``re.findall(r"\\w{2,}", ...)`` 한 번이면
    길이 2 이상의 단어 토큰을 곧장 뽑을 수 있어 substitute → split → filter 의 3 단계
    파이프라인을 한 단계로 줄인다.
    """
    return set(re.findall(r"\w{2,}", normalize_text(text).lower()))


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# 토큰 Jaccard 가 이 임계값을 넘으면 '같은 finding 이 다른 섹션에 중복' 으로 판정한다.
# 0.7 은 의도적으로 보수적 — 같은 의도의 문장도 단어 하나만 바꾸면 0.6 대로 떨어지므로
# false positive 위험을 줄이는 쪽을 택한다. 운영 관찰 후 상향 조정 가능.
DEDUP_SIMILARITY_THRESHOLD = 0.7


def dedupe_across_sections(
    must_fix: list[str],
    suggestions: list[str],
    comments: list[ReviewComment],
    positives: list[str],
) -> tuple[list[str], list[str], list[ReviewComment], list[str]]:
    """7B 모델이 같은 문장을 여러 섹션에 반복 출력하는 패턴 4 를 후처리로 제거한다.

    우선순위(높을수록 보존): comments[] > must_fix > suggestions > positives.
    라인 코멘트를 가장 높은 우선순위로 두는 이유 — 라인 anchor 가 top-level
    텍스트보다 실제 리뷰 UX 에서 가치가 크다. 또한 요약이 must_fix 에 추가된
    뒤 dedup 을 돌리면 요약 원본(comment body) 을 자연히 보존하게 된다.

    낮은 우선순위 섹션의 항목이 높은 우선순위 섹션의 항목과 Jaccard 유사도
    >= DEDUP_SIMILARITY_THRESHOLD 이면 drop 한다.

    중요: ``comments`` 배열 내부에서는 본문 유사도로 항목을 제거하지 않는다.
    같은 보안 패턴이나 누락 케이스가 서로 다른 ``(path, line)`` 에 동시에
    검출될 수 있고, 이때 첫 번째 anchor 만 남기면 작성자가 나머지 위치의
    실제 문제를 놓치게 된다. ``collect_validated_comments`` 가 이미 동일한
    ``(path, line, body)`` 조합을 dedupe 한 상태라 여기서는 모두 보존하고,
    각 코멘트의 토큰만 ``kept_tokens`` 에 누적해 다른 섹션과의 cross-section
    dedup 에 참조용으로만 쓴다.
    """
    kept_tokens: list[set[str]] = []

    def is_new(text: str) -> bool:
        tokens = _tokenize_for_dedup(text)
        if not tokens:
            return True  # 토큰이 거의 없는 매우 짧은 문장은 판정 보류 — 원본 유지.
        for prev in kept_tokens:
            if _jaccard_similarity(tokens, prev) >= DEDUP_SIMILARITY_THRESHOLD:
                return False
        kept_tokens.append(tokens)
        return True

    # comments[] 는 모두 보존하고 토큰만 누적해 다른 섹션의 중복 판정에 참조한다.
    for comment in comments:
        tokens = _tokenize_for_dedup(comment.body)
        if tokens:
            kept_tokens.append(tokens)

    # top-level 섹션은 우선순위 순서대로 소비. comments 의 토큰이 이미 등록돼
    # 있으므로 must_fix 의 동일 본문은 자연히 drop, 그 다음 suggestions, positives.
    deduped_must_fix = [item for item in must_fix if is_new(item)]
    deduped_suggestions = [item for item in suggestions if is_new(item)]
    deduped_positives = [item for item in positives if is_new(item)]

    return deduped_must_fix, deduped_suggestions, list(comments), deduped_positives


def split_legacy_concerns(items: list[str]) -> tuple[list[str], list[str]]:
    """구 스키마의 concerns 를 CONCERN_RISK_MARKERS 포함 여부로 나눈다.

    위험 신호(위험/누락/취약/에러 등)가 있으면 must_fix, 없으면 suggestions 로 흡수한다.
    프롬프트가 새 스키마를 요구하지만 모델이 아직 따라오지 못할 때의 안전망이다.
    """
    must_fix: list[str] = []
    suggestions: list[str] = []
    for item in items:
        if any(marker in item for marker in CONCERN_RISK_MARKERS):
            must_fix.append(item)
        else:
            suggestions.append(item)
    return must_fix, suggestions


def validate_mlx_output(
    result: dict[str, Any],
    files: list[PullRequestFile],
    *,
    log_prefix: str = "",
) -> ValidatedReview:
    """모델 출력을 실제 리뷰 payload로 쓰기 전에 안전하게 정리한다."""
    comments, validation_stats = collect_validated_comments(result, files)
    if log_prefix:
        log_mlx_result_metadata(result, log_prefix)
        log_comment_validation_stats(validation_stats, log_prefix)

    summary = normalize_text(result.get("summary")) or "자동 리뷰를 완료했습니다."
    positives = sanitize_positive_items(normalize_text_list(result.get("positives"), max_items=10))
    raw_must_fix = sanitize_text_items(normalize_text_list(result.get("must_fix"), max_items=10))
    raw_suggestions = sanitize_text_items(normalize_text_list(result.get("suggestions"), max_items=10))
    raw_legacy_concerns = sanitize_text_items(
        normalize_text_list(result.get("legacy_concerns") or result.get("concerns"), max_items=10)
    )
    if log_prefix and (raw_must_fix or raw_suggestions or raw_legacy_concerns):
        log_progress(
            log_prefix,
            "Ignoring model top-level finding buckets as standalone items; "
            "line-anchored high-confidence entries may already be recovered as comments "
            f"must_fix={len(raw_must_fix)} suggestions={len(raw_suggestions)} "
            f"legacy_concerns={len(raw_legacy_concerns)}",
        )

    # 모델의 top-level finding 은 path/line/confidence 계약을 증명할 수 없으므로 게시하지 않는다.
    # GitHub 본문 요약은 아래에서 검증 통과한 라인 코멘트만 바탕으로 다시 만든다.
    must_fix: list[str] = []
    suggestions: list[str] = []

    # 라인 코멘트 요약을 등급별로 다른 버킷에 태운다. Blocking/Major 요약은 상단
    # must_fix 에, Minor/Suggestion 요약은 suggestions 에 남겨 훑을 때 우선순위가
    # 유지되도록 한다. 규칙 기반 감지기(서명 우회, 비밀값 로그) 는 명시적으로
    # Blocking / Major 를 붙이므로 반드시 must_fix 쪽으로 가 REQUEST_CHANGES 를 유도한다.
    blocking_comments = [c for c in comments if c.severity in BLOCKING_SEVERITIES]
    non_blocking_comments = [c for c in comments if c.severity not in BLOCKING_SEVERITIES]
    must_fix_summaries = summarize_comment_bodies(blocking_comments, max_items=3)
    suggestion_summaries = summarize_comment_bodies(non_blocking_comments, max_items=3)
    must_fix = merge_distinct_items(must_fix, must_fix_summaries, max_items=5)
    suggestions = merge_distinct_items(suggestions, suggestion_summaries, max_items=5)

    # 패턴 4 방어: 같은 finding 이 여러 섹션에 그대로 반복되는 환각을 후처리로 제거.
    # 우선순위는 comments[] > must_fix > suggestions > positives. dedup 은 comments
    # 자체는 변경하지 않고 (라인 anchor 보존을 위해 다른 path/line 의 동일 본문도 유지)
    # top-level 섹션의 중복만 제거하므로 blocking_comments 는 재계산 불필요.
    must_fix, suggestions, comments, positives = dedupe_across_sections(
        must_fix, suggestions, comments, positives
    )

    # 차단성 신호(must_fix 또는 Blocking/Major 라인 코멘트) 가 있을 때만
    # REQUEST_CHANGES 로 승격한다. 순수 Minor/Suggestion 코멘트만 있으면 COMMENT 유지.
    should_request_changes = bool(must_fix or blocking_comments)
    # summary 재작성 등은 '뭐라도 남길 내용이 있는지' 로 판단하므로 has_findings 는 별도.
    has_findings = bool(comments or must_fix or suggestions)
    # APPROVE 는 '지적이 전혀 없을 때만' 부여한다. suggestions 나 Minor 라인 코멘트가
    # 있으면 검토는 끝났지만 완전 승인은 아니므로 COMMENT 로 남긴다.
    event = decide_review_event(
        should_request_changes=should_request_changes,
        has_any_finding=has_findings,
    )

    if not has_findings:
        summary = sanitize_summary(summary, has_findings=False)
    else:
        summary = sanitize_summary(summary, has_findings=True)

    return ValidatedReview(
        comments=comments,
        summary=summary,
        event=event,
        positives=positives,
        must_fix=must_fix,
        suggestions=suggestions,
    )


def extract_model_name_from_result(mlx_result: dict[str, Any]) -> str | None:
    """_meta 가 dict 가 아니거나 model_name 이 문자열이 아니면 안전하게 None 을 돌려준다.

    커스텀 MLX 클라이언트나 예외 경로에서 _meta 가 비정상 타입으로 실려올 수 있어,
    GitHub 리뷰 body 조립 단계에서는 사이드이펙트 없이 푸터를 생략할 수 있게 한다.
    """
    metadata = mlx_result.get("_meta")
    if not isinstance(metadata, dict):
        return None
    model_name = metadata.get("model_name")
    if not isinstance(model_name, str):
        return None
    return model_name


def build_review_payload(
    summary: str,
    event: str,
    comments: list[ReviewComment],
    positives: list[str],
    must_fix: list[str],
    suggestions: list[str],
    *,
    model_name: str | None = None,
    existing_review_context: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """GitHub Review API가 기대하는 본문/인라인 코멘트 구조를 만든다.

    본문은 MLX 리뷰와 Copilot 리뷰를 분리해서 출처를 명확히 한다. MLX 섹션 안에서는
    '반드시 수정할 사항 → 권장 개선사항 → 개선된 점' 순서로 배치해 차단성 항목이
    먼저 보이게 한다. Copilot 섹션은 이미 PR에 달린 Copilot 코멘트 요약만 표시한다.
    """
    body_lines: list[str] = ["## MLX 리뷰", "", normalize_text(summary) or DEFAULT_NO_FINDINGS_SUMMARY]

    if must_fix:
        body_lines.extend(["", "### 반드시 수정할 사항"])
        body_lines.extend(f"- {item}" for item in must_fix)

    if suggestions:
        body_lines.extend(["", "### 권장 개선사항"])
        body_lines.extend(f"- {item}" for item in suggestions)

    if positives:
        body_lines.extend(["", "### 개선된 점"])
        body_lines.extend(f"- {item}" for item in positives)

    body_lines.extend(["", "### 라인 단위 코멘트"])
    if comments:
        body_lines.append(f"- 자동 리뷰에서 {len(comments)}개의 라인 단위 개선 사항을 남겼습니다.")
    else:
        body_lines.append("- 라인 단위로 남길 개선 사항은 발견되지 않았습니다.")

    body_lines.extend(build_copilot_review_section(existing_review_context))

    # 어떤 모델 구성이 이 리뷰를 생성했는지 추적하기 위한 푸터. 모델 정보가 없는 경우는 생략.
    # normalize_text 는 None, 빈 문자열, 공백만 있는 값을 모두 "" 로 정규화하므로 별도 가드가 필요 없다.
    normalized_model_name = normalize_text(model_name)
    if normalized_model_name:
        body_lines.extend(
            [
                "",
                "---",
                f"<sub>사용된 모델: {normalized_model_name}</sub>",
            ]
        )

    return {
        "body": "\n".join(body_lines),
        "event": event,
        "comments": [
            {
                "path": comment.path,
                "line": comment.line,
                "side": comment.side,
                # GitHub 라인 코멘트 본문 맨 앞에 '[Blocking]' 같은 등급 태그를 붙여
                # 훑는 사람이 심각도를 즉시 구분할 수 있게 한다.
                "body": f"[{comment.severity}] {comment.body}\n\nConfidence score: {comment.confidence:.2f}",
            }
            for comment in comments
        ],
    }


def should_retry_review_as_comment(error: RuntimeError, payload: dict[str, Any]) -> bool:
    """자기 PR에 REQUEST_CHANGES를 달 수 없는 경우만 안전하게 재시도한다."""
    if payload.get("event") != "REQUEST_CHANGES":
        return False

    message = normalize_text(str(error)).lower()
    return "request changes on your own pull request" in message


def build_review_result(
    repository: str,
    pull_number: int,
    validated_review: ValidatedReview,
    payload: dict[str, Any],
    auth_source: str | None,
) -> dict[str, Any]:
    """로그와 후속 처리에서 재사용할 리뷰 결과 요약을 만든다.

    Phase 2 이후 ValidatedReview 는 concerns 필드를 갖지 않고 must_fix / suggestions
    로 분리돼 있다. 호환성을 위해 concern_count = must_fix + suggestions 합으로
    계속 노출하면서, 심각도별 카운트도 함께 제공한다.
    """
    must_fix_count = len(validated_review.must_fix)
    suggestion_count = len(validated_review.suggestions)
    return {
        "status": "completed",
        "repository": repository,
        "pull_number": pull_number,
        "summary": validated_review.summary,
        "event": validated_review.event,
        "comment_count": len(validated_review.comments),
        "positive_count": len(validated_review.positives),
        "must_fix_count": must_fix_count,
        "suggestion_count": suggestion_count,
        "concern_count": must_fix_count + suggestion_count,
        "payload": payload,
        "auth_source": auth_source or "personal_access_token",
    }


def build_review_message(
    *,
    posted_event: str,
    comments: list[ReviewComment],
    payload: dict[str, Any],
    response: Any,
    fallback_note: str,
) -> str:
    """최종 콘솔 로그와 반환 메시지에 공통으로 쓰는 본문을 만든다."""
    message_lines = [
        "리뷰 등록이 완료되었습니다.",
        f"리뷰 ID: {response.get('id')}",
        f"이벤트: {posted_event}",
        f"라인 코멘트 수: {len(comments)}",
        "",
        payload["body"],
    ]
    if fallback_note:
        message_lines[1:1] = [fallback_note]
    if comments:
        message_lines.extend(
            [
                "",
                "라인 코멘트:",
                *(f"- {comment.path}:{comment.line} {comment.body}" for comment in comments),
            ]
        )
    return "\n".join(message_lines)


def load_patchable_pr_files_result(
    github: GitHubApi,
    pull_number: int,
    *,
    log_prefix: str = "",
) -> PullRequestFileLoadResult:
    log_progress(log_prefix, f"Fetching PR files for {github.repository}#{pull_number}")
    raw_files = github.list_pr_files(pull_number)
    pr_files = build_pr_files(raw_files)
    if not pr_files:
        log_progress(log_prefix, "Loaded 0 patchable file(s)")
        return PullRequestFileLoadResult(files=[], patchable_count=0)

    config = load_reviewbot_config(github, pull_number, log_prefix=log_prefix)
    filtered_files, skipped_by_reviewbot = filter_reviewbot_files(pr_files, config)
    if config.loaded:
        log_progress(
            log_prefix,
            f"Loaded {len(filtered_files)} patchable file(s) after {REVIEWBOT_CONFIG_PATH} filters "
            f"(skipped {skipped_by_reviewbot} of {len(pr_files)})",
        )
    elif skipped_by_reviewbot:
        log_progress(
            log_prefix,
            f"Loaded {len(filtered_files)} patchable file(s) after built-in generated-file filters "
            f"(skipped {skipped_by_reviewbot} of {len(pr_files)})",
        )
    else:
        log_progress(log_prefix, f"Loaded {len(filtered_files)} patchable file(s)")
    context_settings = configured_review_context_settings()
    enrich_pr_files_with_current_context(
        github,
        pull_number,
        filtered_files,
        settings=context_settings,
        log_prefix=log_prefix,
    )
    repository_context = collect_repository_context(
        github,
        pull_number,
        filtered_files,
        config,
        settings=context_settings,
        log_prefix=log_prefix,
    )
    return PullRequestFileLoadResult(
        files=filtered_files,
        patchable_count=len(pr_files),
        repository_context=repository_context,
        skipped_by_reviewbot=skipped_by_reviewbot,
        reviewbot_config_loaded=config.loaded,
        default_filter_applied=not config.loaded and config.has_filters,
    )


def load_patchable_pr_files(github: GitHubApi, pull_number: int, *, log_prefix: str = "") -> list[PullRequestFile]:
    return load_patchable_pr_files_result(github, pull_number, log_prefix=log_prefix).files


def generate_review_artifacts(
    repository: str,
    pull_number: int,
    pr_files: list[PullRequestFile],
    *,
    repository_context: list[RepositoryContextEntry] | None = None,
    existing_review_context: list[dict[str, Any]] | None = None,
    log_prefix: str = "",
) -> ReviewGenerationArtifacts:
    prompt = make_prompt(
        repository,
        pull_number,
        pr_files,
        repository_context=repository_context,
        existing_review_context=existing_review_context,
    )
    if os.environ.get("WRITE_PROMPT_DEBUG") == "1":
        write_prompt_debug_file(prompt)

    prompt_max_chars = configured_review_prompt_max_chars()
    if should_split_review_prompt(prompt, pr_files, prompt_max_chars):
        return generate_batched_review_artifacts(
            repository,
            pull_number,
            pr_files,
            existing_review_context=existing_review_context,
            prompt_max_chars=prompt_max_chars,
            initial_prompt_chars=len(prompt),
            log_prefix=log_prefix,
        )

    mlx_started_at = time.monotonic()
    log_progress(log_prefix, "Running MLX review model")
    try:
        mlx_result = run_mlx(prompt, log_prefix=log_prefix)
    except RuntimeError as exc:
        if not should_retry_as_batched_review(exc, pr_files):
            raise
        retry_prompt_max_chars = review_prompt_retry_budget(prompt_max_chars, len(prompt), exc)
        return generate_batched_review_artifacts(
            repository,
            pull_number,
            pr_files,
            existing_review_context=existing_review_context,
            prompt_max_chars=retry_prompt_max_chars,
            initial_prompt_chars=len(prompt),
            log_prefix=log_prefix,
        )
    log_progress(log_prefix, f"MLX review completed in {time.monotonic() - mlx_started_at:.1f}s")
    validated_review = validate_mlx_output(mlx_result, pr_files, log_prefix=log_prefix)
    payload = build_review_payload(
        validated_review.summary,
        validated_review.event,
        validated_review.comments,
        validated_review.positives,
        validated_review.must_fix,
        validated_review.suggestions,
        model_name=extract_model_name_from_result(mlx_result),
        existing_review_context=existing_review_context,
    )
    return ReviewGenerationArtifacts(
        prompt=prompt,
        mlx_result=mlx_result,
        validated_review=validated_review,
        payload=payload,
    )


def should_split_review_prompt(prompt: str, pr_files: list[PullRequestFile], prompt_max_chars: int) -> bool:
    """원격 generate 상한에 걸릴 큰 PR은 파일 batch로 나눠 리뷰한다."""
    return prompt_max_chars > 0 and len(pr_files) > 1 and len(prompt) > prompt_max_chars


def is_mlx_prompt_too_large_error(error: RuntimeError) -> bool:
    message = normalize_text(str(error)).lower()
    return (
        "http 413" in message
        or "message content too large" in message
        or "generate request body is too large" in message
    )


def parse_prompt_limit_from_mlx_error(error: RuntimeError) -> int | None:
    """MLX 413 메시지의 ``current > limit`` 숫자에서 서버 상한을 읽는다."""
    message = normalize_text(str(error))
    match = re.search(r"\b\d+\s*>\s*(\d+)\s*chars?\b", message)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def review_prompt_retry_budget(configured_budget: int, initial_prompt_chars: int, error: RuntimeError) -> int:
    """413 이후에는 실패한 prompt보다 확실히 작은 예산으로 batch를 강제한다."""
    base_budget = configured_budget if configured_budget > 0 else DEFAULT_REVIEW_PROMPT_MAX_CHARS
    retry_budget = min(base_budget, DEFAULT_REVIEW_PROMPT_MAX_CHARS)

    server_limit = parse_prompt_limit_from_mlx_error(error)
    if server_limit is not None:
        retry_budget = min(retry_budget, max(server_limit - 5_000, 1))
    else:
        retry_budget = min(retry_budget, max(initial_prompt_chars // 2, 1))

    return max(retry_budget, 1)


def should_retry_as_batched_review(error: RuntimeError, pr_files: list[PullRequestFile]) -> bool:
    return len(pr_files) > 1 and is_mlx_prompt_too_large_error(error)


def prompt_truncated_context_mode(context_mode: str) -> str:
    normalized = normalize_text(context_mode) or "current_file_context"
    if normalized.endswith("_prompt_truncated"):
        return normalized
    return f"{normalized}_prompt_truncated"


def fit_pr_file_to_prompt_budget(
    repository: str,
    pull_number: int,
    pr_file: PullRequestFile,
    *,
    existing_review_context: list[dict[str, Any]] | None,
    prompt_max_chars: int,
) -> PullRequestFile:
    """단일 파일 prompt도 예산을 넘으면 current_file_context만 명시적으로 줄인다."""
    if prompt_max_chars <= 0:
        return pr_file

    fitted_file = pr_file
    for _attempt in range(4):
        prompt = make_prompt(
            repository,
            pull_number,
            [fitted_file],
            repository_context=None,
            existing_review_context=existing_review_context,
        )
        if len(prompt) <= prompt_max_chars:
            return fitted_file

        current_context = fitted_file.current_file_context
        if not current_context:
            return fitted_file

        overage = len(prompt) - prompt_max_chars
        target_context_chars = len(current_context) - overage - 1_000
        if target_context_chars <= 0:
            next_context = ""
            next_mode = "current_file_context_omitted_to_fit_prompt"
        else:
            next_context = truncate_context(
                current_context,
                target_context_chars,
                suffix="current file context truncated to fit prompt budget",
            )
            next_mode = prompt_truncated_context_mode(fitted_file.current_file_context_mode)

        if next_context == current_context:
            return fitted_file
        fitted_file = replace(
            fitted_file,
            current_file_context=next_context,
            current_file_context_mode=next_mode,
        )

    return fitted_file


def split_pr_files_for_prompt_budget(
    repository: str,
    pull_number: int,
    pr_files: list[PullRequestFile],
    *,
    existing_review_context: list[dict[str, Any]] | None,
    prompt_max_chars: int,
) -> list[list[PullRequestFile]]:
    """각 batch prompt가 예산에 들어오도록 PR 파일 순서를 유지해 나눈다."""
    if prompt_max_chars <= 0:
        return [list(pr_files)]

    batches: list[list[PullRequestFile]] = []
    current_batch: list[PullRequestFile] = []

    for pr_file in pr_files:
        budgeted_file = fit_pr_file_to_prompt_budget(
            repository,
            pull_number,
            pr_file,
            existing_review_context=existing_review_context,
            prompt_max_chars=prompt_max_chars,
        )
        candidate = [*current_batch, budgeted_file]
        candidate_prompt = make_prompt(
            repository,
            pull_number,
            candidate,
            repository_context=None,
            existing_review_context=existing_review_context,
        )
        if current_batch and len(candidate_prompt) > prompt_max_chars:
            batches.append(current_batch)
            current_batch = [budgeted_file]
            continue
        current_batch = candidate

    if current_batch:
        batches.append(current_batch)
    return batches


def dedupe_review_comments(comments: list[ReviewComment]) -> list[ReviewComment]:
    seen: set[tuple[str, int, str]] = set()
    deduped: list[ReviewComment] = []
    for comment in comments:
        key = (comment.path, comment.line, normalize_text(comment.body))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(comment)
    return deduped


def combine_batched_reviews(batch_artifacts: list[ReviewGenerationArtifacts]) -> ValidatedReview:
    comments = dedupe_review_comments(
        [comment for artifact in batch_artifacts for comment in artifact.validated_review.comments]
    )
    positives = merge_distinct_items(
        [],
        [item for artifact in batch_artifacts for item in artifact.validated_review.positives],
        max_items=10,
    )
    must_fix = merge_distinct_items(
        [],
        [item for artifact in batch_artifacts for item in artifact.validated_review.must_fix],
        max_items=10,
    )
    suggestions = merge_distinct_items(
        [],
        [item for artifact in batch_artifacts for item in artifact.validated_review.suggestions],
        max_items=10,
    )
    summaries = merge_distinct_items(
        [],
        [artifact.validated_review.summary for artifact in batch_artifacts],
        max_items=3,
    )
    has_findings = bool(comments or must_fix or suggestions)
    blocking_comments = [comment for comment in comments if comment.severity in BLOCKING_SEVERITIES]
    event = decide_review_event(
        should_request_changes=bool(must_fix or blocking_comments),
        has_any_finding=has_findings,
    )
    summary_prefix = f"대형 PR이라 {len(batch_artifacts)}개 묶음으로 나눠 리뷰했습니다."
    if has_findings:
        summaries = [summary for summary in summaries if not looks_like_no_findings_summary(summary)]

    if summaries:
        summary = f"{summary_prefix} {' '.join(summaries)}"
    else:
        summary = summary_prefix
    summary = sanitize_summary(summary, has_findings=has_findings)
    if not summary.startswith(summary_prefix):
        summary = f"{summary_prefix} {summary}"
    return ValidatedReview(
        comments=comments,
        summary=summary,
        event=event,
        positives=positives,
        must_fix=must_fix,
        suggestions=suggestions,
    )


def generate_batched_review_artifacts(
    repository: str,
    pull_number: int,
    pr_files: list[PullRequestFile],
    *,
    existing_review_context: list[dict[str, Any]] | None,
    prompt_max_chars: int,
    initial_prompt_chars: int,
    log_prefix: str = "",
) -> ReviewGenerationArtifacts:
    """큰 PR을 여러 요청으로 나눠 generate 서버 입력 상한을 넘지 않게 한다."""
    batches = split_pr_files_for_prompt_budget(
        repository,
        pull_number,
        pr_files,
        existing_review_context=existing_review_context,
        prompt_max_chars=prompt_max_chars,
    )
    log_progress(
        log_prefix,
        f"Prompt size {initial_prompt_chars} exceeds budget {prompt_max_chars}; "
        f"reviewing in {len(batches)} batch(es) without repository_context",
    )

    batch_artifacts: list[ReviewGenerationArtifacts] = []
    for index, batch_files in enumerate(batches, start=1):
        batch_prompt = make_prompt(
            repository,
            pull_number,
            batch_files,
            repository_context=None,
            existing_review_context=existing_review_context,
        )
        mlx_started_at = time.monotonic()
        log_progress(
            log_prefix,
            f"Running MLX review model for batch {index}/{len(batches)} "
            f"files={len(batch_files)} prompt_chars={len(batch_prompt)}",
        )
        mlx_result = run_mlx(batch_prompt, log_prefix=log_prefix)
        log_progress(
            log_prefix,
            f"MLX review batch {index}/{len(batches)} completed in {time.monotonic() - mlx_started_at:.1f}s",
        )
        validated_review = validate_mlx_output(mlx_result, batch_files, log_prefix=log_prefix)
        payload = build_review_payload(
            validated_review.summary,
            validated_review.event,
            validated_review.comments,
            validated_review.positives,
            validated_review.must_fix,
            validated_review.suggestions,
            model_name=extract_model_name_from_result(mlx_result),
            existing_review_context=existing_review_context,
        )
        batch_artifacts.append(
            ReviewGenerationArtifacts(
                prompt=batch_prompt,
                mlx_result=mlx_result,
                validated_review=validated_review,
                payload=payload,
            )
        )

    combined_review = combine_batched_reviews(batch_artifacts)
    combined_result = dict(batch_artifacts[-1].mlx_result)
    combined_meta = dict(combined_result.get("_meta") or {})
    combined_meta["review_batches"] = len(batch_artifacts)
    combined_result["_meta"] = combined_meta
    combined_payload = build_review_payload(
        combined_review.summary,
        combined_review.event,
        combined_review.comments,
        combined_review.positives,
        combined_review.must_fix,
        combined_review.suggestions,
        model_name=extract_model_name_from_result(combined_result),
        existing_review_context=existing_review_context,
    )
    return ReviewGenerationArtifacts(
        prompt="\n\n".join(artifact.prompt for artifact in batch_artifacts),
        mlx_result=combined_result,
        validated_review=combined_review,
        payload=combined_payload,
    )


def post_review_with_fallback(
    github: GitHubApi,
    pull_number: int,
    *,
    payload: dict[str, Any],
    requested_event: str,
    log_prefix: str = "",
) -> PostedReviewResult:
    posted_event = requested_event
    fallback_note = ""
    requested_event_after_retry: str | None = None
    try:
        log_progress(log_prefix, f"Posting GitHub review as {requested_event}")
        response = github.post_review(pull_number, payload)
    except RuntimeError as exc:
        if not should_retry_review_as_comment(exc, payload):
            raise

        retry_payload = dict(payload)
        retry_payload["event"] = "COMMENT"
        log_progress(log_prefix, "Retrying review post as COMMENT because REQUEST_CHANGES was rejected")
        response = github.post_review(pull_number, retry_payload)
        payload = retry_payload
        posted_event = "COMMENT"
        requested_event_after_retry = requested_event
        fallback_note = "본인 PR에는 REQUEST_CHANGES를 남길 수 없어 COMMENT로 다시 등록했습니다."

    return PostedReviewResult(
        response=response,
        posted_event=posted_event,
        payload=payload,
        fallback_note=fallback_note,
        requested_event=requested_event_after_retry,
    )


def review_pull_request(
    repository: str,
    pull_number: int,
    token: str,
    api_url: str = DEFAULT_API_URL,
    dry_run: bool = False,
    auth_source: str | None = None,
    log_prefix: str = "",
) -> dict[str, Any]:
    """PR diff를 수집하고 모델 리뷰를 생성한 뒤 GitHub에 등록한다."""
    started_at = time.monotonic()
    github = GitHubApi(token=token, repository=repository, api_url=api_url)
    file_load_result = load_patchable_pr_files_result(github, pull_number, log_prefix=log_prefix)
    pr_files = file_load_result.files

    if not pr_files:
        reason = "No patchable files found."
        if file_load_result.reviewbot_config_loaded and file_load_result.patchable_count > 0:
            reason = f"No reviewable files after {REVIEWBOT_CONFIG_PATH} filters."
        elif file_load_result.default_filter_applied and file_load_result.patchable_count > 0:
            reason = "No reviewable files after built-in generated-file filters."
        return {
            "status": "skipped",
            "reason": reason,
            "repository": repository,
            "pull_number": pull_number,
            "skipped_by_reviewbot": file_load_result.skipped_by_reviewbot,
        }

    existing_review_context = load_existing_review_context(github, pull_number, log_prefix=log_prefix)
    copilot_review_request = (
        build_copilot_review_request_result(status="dry_run", reviewer=normalize_copilot_reviewer())
        if dry_run
        else maybe_request_copilot_review(
            github,
            pull_number,
            existing_review_context=existing_review_context,
            log_prefix=log_prefix,
        )
    )
    artifacts = generate_review_artifacts(
        repository,
        pull_number,
        pr_files,
        repository_context=file_load_result.repository_context,
        existing_review_context=existing_review_context,
        log_prefix=log_prefix,
    )
    result = build_review_result(
        repository,
        pull_number,
        artifacts.validated_review,
        artifacts.payload,
        auth_source,
    )
    result["existing_review_context_count"] = len(existing_review_context)
    result["repository_context_count"] = len(file_load_result.repository_context)
    result["copilot_review_request"] = copilot_review_request

    if dry_run:
        log_progress(log_prefix, f"Dry run completed in {time.monotonic() - started_at:.1f}s")
        return result

    posted = post_review_with_fallback(
        github,
        pull_number,
        payload=artifacts.payload,
        requested_event=artifacts.validated_review.event,
        log_prefix=log_prefix,
    )
    if posted.requested_event is not None:
        result["requested_event"] = posted.requested_event
        result["event"] = posted.posted_event
        result["payload"] = posted.payload

    result["review_id"] = posted.response.get("id")
    result["message"] = build_review_message(
        posted_event=posted.posted_event,
        comments=artifacts.validated_review.comments,
        payload=posted.payload,
        response=posted.response,
        fallback_note=posted.fallback_note,
    )
    log_progress(log_prefix, f"Review posted successfully in {time.monotonic() - started_at:.1f}s")
    return result
