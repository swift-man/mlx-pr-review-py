#!/usr/bin/env python3
"""Shared PR review service used by CLI entrypoints and the webhook server."""

from __future__ import annotations

import json
import os
import re
import signal
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import certifi
import jwt


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_CA_BUNDLE_ENV = "GITHUB_CA_BUNDLE"
DEFAULT_NO_FINDINGS_SUMMARY = (
    "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
)
DEFAULT_FINDINGS_SUMMARY = "자동 리뷰에서 확인이 필요한 변경 사항이 발견되었습니다. 아래 코멘트와 개선점을 확인해 주세요."
NO_FINDINGS_SUMMARY_MARKERS = (
    DEFAULT_NO_FINDINGS_SUMMARY,
    "즉시 수정이 필요한 문제는 보이지 않습니다.",
    "검토할 만한 문제를 찾지 못했습니다.",
    "지적할 만한 문제는 보이지 않습니다.",
    "별도 개선 필요 사항은 발견되지 않았습니다.",
    "개선이 필요한 점은 발견되지 않았습니다.",
)
DEFAULT_FALLBACK_POSITIVES = [
    "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
]
DEFAULT_NO_CONCERNS_TEXT = "이번 diff 기준으로 별도 개선 필요 사항은 발견되지 않았습니다."
LOW_SIGNAL_POSITIVE_MARKERS = (
    "pr diff가 잘 작성",
    "pr diff의 내용이 잘 정리",
    "변경 내용이 잘 정리",
    "모든 파일이 잘 수정",
)
LOW_SIGNAL_MODEL_CHANGE_MARKERS = (
    "mlx_model의 값이 변경",
    "mlx_model의 값이 업데이트",
    "새로운 모델이 적합한지 확인",
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
    "좋습니다",
    "적절합니다",
    "개선되었습니다",
)
NO_CONCERN_TEXTS = {
    DEFAULT_NO_CONCERNS_TEXT,
    "별도 개선 필요 사항은 발견되지 않았습니다.",
    "개선이 필요한 점은 발견되지 않았습니다.",
    "개선이 필요한 점은 없습니다.",
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
PROMPT_ECHO_MARKERS = (
    "review_runner/",
    "valid_comment_lines",
    "RIGHT-side",
    "response_schema",
    "style-only",
    "praise-only",
)

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

    if looks_like_generic_positive(normalized):
        return True

    lowered = normalized.lower()
    return any(
        marker in lowered
        for marker in (
            "핵심 변경 의도가 diff 안에서 비교적 명확하게 드러납니다.",
            "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
        )
    )


def build_ssl_context() -> ssl.SSLContext:
    """GitHub API 호출에 사용할 CA 번들을 환경변수와 certifi에서 순서대로 찾는다."""
    cafile = os.environ.get(DEFAULT_CA_BUNDLE_ENV) or os.environ.get("SSL_CERT_FILE") or certifi.where()
    return ssl.create_default_context(cafile=cafile)


def request_json_url(
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
    ssl_context: ssl.SSLContext | None = None,
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
        with urllib.request.urlopen(request, context=ssl_context or build_ssl_context()) as response:
            raw = response.read().decode("utf-8")
            if not raw:
                return None
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API {method} {url} failed: {exc.code} {message}") from exc
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


@dataclass
class ReviewComment:
    path: str
    line: int
    body: str
    side: str = "RIGHT"


@dataclass
class PullRequestFile:
    filename: str
    status: str
    patch: str
    additions: int
    deletions: int
    right_side_lines: set[int]


@dataclass
class ValidatedReview:
    """모델 출력과 규칙 기반 검사를 합쳐서 정규화한 리뷰 결과다."""

    comments: list[ReviewComment]
    summary: str
    event: str
    positives: list[str]
    concerns: list[str]


@dataclass
class CommentValidationStats:
    raw_model_comments: int = 0
    accepted_model_comments: int = 0
    dropped_model_comment_reasons: dict[str, int] = field(default_factory=dict)
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

    def request_json(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
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

    def post_review(self, pull_number: int, body: dict[str, Any]) -> Any:
        return self.request_json(
            "POST",
            f"/repos/{self.repository}/pulls/{pull_number}/reviews",
            body=body,
        )


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


def summarize_comment_bodies(comments: list[ReviewComment], max_items: int = 3) -> list[str]:
    summaries: list[str] = []
    seen: set[str] = set()

    for comment in comments:
        first_line = comment.body.strip().splitlines()[0] if comment.body.strip() else ""
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
                            body=(
                                "서명 헤더가 없을 때 바로 반환하면 서명 검증이 건너뛰어져 위조된 웹훅도 처리될 수 있습니다. "
                                "누락된 서명은 401로 거부하도록 유지하세요."
                            ),
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
                            body=(
                                "서명 값이 없을 때 요청을 통과시키고 있어 인증되지 않은 웹훅을 받아들이게 됩니다. "
                                "서명 누락이나 불일치는 예외를 발생시켜 요청을 거부해야 합니다."
                            ),
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
        body = (
            "토큰이나 secret 값을 로그에 남기면 서버 로그 접근만으로 인증 정보가 유출될 수 있습니다. "
            "민감한 값은 출력하지 말고, 필요하면 마스킹된 메타데이터만 기록하세요."
        )
    else:
        body = (
            "이 파일에서 토큰이나 secret 값을 로그에 남기는 코드가 여러 곳 추가되었습니다. "
            "민감한 값은 출력하지 말고, 필요하면 마스킹된 메타데이터만 기록하세요."
        )

    return [
        ReviewComment(
            path=pr_file.filename,
            line=first_match_line,
            body=body,
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
                    body=(
                        f"`{typo}` 오타 때문에 기존 계약에서 기대하는 `{expected}` 키나 헤더를 찾지 못해 호출 흐름이 깨질 수 있습니다. "
                        f"공개 응답 필드와 GitHub 헤더 이름은 `{expected}`로 정확히 유지하세요."
                    ),
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
        for detector in detectors:
            for comment in detector(pr_file):
                key = (comment.path, comment.line, comment.body)
                if key in seen:
                    continue
                seen.add(key)
                comments.append(comment)

    return comments


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


def make_prompt(repository: str, pull_number: int, files: list[PullRequestFile]) -> str:
    """모델이 바로 읽을 수 있는 JSON 프롬프트를 조립한다."""
    prompt_payload = {
        "repository": repository,
        "pull_request": pull_number,
        "instructions": {
            "task": "이 PR diff를 리뷰하고, 실제로 수정이 필요한 문제를 구체적으로 알려주세요.",
            "language_rules": [
                "summary, positives, concerns, comments의 모든 문장은 반드시 한국어로 작성하세요.",
                "톤은 전문적이고 간결하게 유지하세요.",
                "칭찬은 positives에만 작성하고, 라인 코멘트에는 작성하지 마세요.",
            ],
            "json_rules": [
                "최상위 키는 summary, event, positives, concerns, comments만 사용하세요.",
                "positives와 concerns는 반드시 JSON 배열로 반환하세요.",
                "summary 문자열 안에 positive1:, concerns1:, comments: 같은 라벨을 섞어 쓰지 마세요.",
                "event 값은 COMMENT 또는 REQUEST_CHANGES 중 하나만 사용하세요.",
            ],
            "line_comment_rules": [
                "라인 코멘트는 실제 diff에서 보이는 문제만 지적하세요.",
                "반드시 각 파일의 valid_comment_lines 안에 있는 RIGHT-side line 번호만 사용하세요.",
                "정확성, 보안, 안정성, 신뢰성, 성능, 중요한 유지보수성 문제를 우선하세요.",
                "스타일-only 코멘트나 칭찬-only 코멘트는 금지합니다.",
                "각 코멘트에는 왜 문제인지와 어떻게 고치면 좋은지를 한국어로 짧고 분명하게 적으세요.",
            ],
            "summary_rules": [
                "summary는 전체 변경을 한두 문장으로 요약하세요.",
                "positives에는 좋은 점을 1~3개 정도 작성하세요.",
                "concerns에는 개선이 필요한 점을 0~3개 정도 작성하세요.",
                "문제가 없더라도 positives는 반드시 1개 이상 작성하세요.",
                "라인 코멘트와 summary/concerns 내용은 diff에 근거해야 합니다.",
                "파일별 추가/삭제/변경 개수나 line 번호를 summary에 나열하지 마세요.",
            ],
            "response_schema": {
                "summary": "짧은 전체 리뷰 요약 (한국어)",
                "event": "COMMENT 또는 REQUEST_CHANGES",
                "positives": [
                    "좋은 점 한 항목 (한국어 문자열)",
                ],
                "concerns": [
                    "개선이 필요한 점 한 항목 (한국어 문자열)",
                ],
                "comments": [
                    {
                        "path": "relative/file.py",
                        "line": 12,
                        "body": "왜 문제인지와 어떻게 수정하면 좋은지 설명하는 한국어 코멘트",
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
            }
            for f in files
        ],
    }
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
    command = configured_mlx_review_command()
    lock_acquired = _MLX_RUN_LOCK.acquire(blocking=False)
    if not lock_acquired:
        log_progress(log_prefix, "Another MLX review is already running; waiting for the shared model slot")
        _MLX_RUN_LOCK.acquire()

    try:
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
        f"rule_based_added={stats.rule_based_added} "
        f"rule_based_duplicates={stats.rule_based_duplicates} "
        f"dropped_after_validation={format_reason_counts(stats.dropped_model_comment_reasons)}",
    )


def collect_validated_comments(
    result: dict[str, Any],
    files: list[PullRequestFile],
) -> tuple[list[ReviewComment], CommentValidationStats]:
    """모델 코멘트와 규칙 기반 코멘트를 합치고 중복을 제거한다."""
    file_index = {f.filename: f for f in files}
    comments: list[ReviewComment] = []
    seen_comment_keys: set[tuple[str, int, str]] = set()
    raw_comments = result.get("comments", [])
    stats = CommentValidationStats(raw_model_comments=len(raw_comments) if isinstance(raw_comments, list) else 0)

    for raw in raw_comments if isinstance(raw_comments, list) else []:
        path = raw.get("path")
        line = raw.get("line")
        body = normalize_text(raw.get("body"))
        if not path:
            increment_reason(stats.dropped_model_comment_reasons, "missing_path")
            continue
        if not isinstance(line, int):
            increment_reason(stats.dropped_model_comment_reasons, "invalid_line_type")
            continue
        if not body:
            increment_reason(stats.dropped_model_comment_reasons, "empty_body")
            continue
        if looks_like_praise_only_comment(body):
            increment_reason(stats.dropped_model_comment_reasons, "style_or_praise_only")
            continue

        pr_file = file_index.get(path)
        # GitHub Review API는 실제 patch의 RIGHT-side 라인만 허용하므로 여기서 엄격하게 거른다.
        if pr_file is None:
            increment_reason(stats.dropped_model_comment_reasons, "path_mismatch")
            continue
        if line not in pr_file.right_side_lines:
            increment_reason(stats.dropped_model_comment_reasons, "invalid_right_side_line")
            continue

        key = (path, line, body)
        if key in seen_comment_keys:
            increment_reason(stats.dropped_model_comment_reasons, "duplicate_model_comment")
            continue
        seen_comment_keys.add(key)
        comments.append(ReviewComment(path=path, line=line, body=body))
        stats.accepted_model_comments += 1

    for comment in detect_rule_based_comments(files):
        key = (comment.path, comment.line, comment.body)
        if key in seen_comment_keys:
            stats.rule_based_duplicates += 1
            continue
        seen_comment_keys.add(key)
        comments.append(comment)
        stats.rule_based_added += 1

    return comments, stats


def decide_review_event(raw_event: Any, *, has_findings: bool) -> str:
    """모델 event가 어색해도 최종 리뷰 이벤트를 일관되게 정한다."""
    event = normalize_text(raw_event).upper()
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        return "REQUEST_CHANGES" if has_findings else "COMMENT"
    if has_findings:
        return "REQUEST_CHANGES"
    return "COMMENT"


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
    concerns = sanitize_text_items(normalize_text_list(result.get("concerns"), max_items=10))
    comment_summaries = summarize_comment_bodies(comments, max_items=3)
    concerns = merge_distinct_items(concerns, comment_summaries, max_items=3)
    has_findings = bool(comments or concerns)
    event = decide_review_event(result.get("event"), has_findings=has_findings)

    if not has_findings:
        summary = sanitize_summary(summary, has_findings=False)
        if not positives:
            positives = list(DEFAULT_FALLBACK_POSITIVES)
    else:
        summary = sanitize_summary(summary, has_findings=True)
        if not positives:
            positives = ["핵심 변경 의도가 diff 안에서 비교적 명확하게 드러납니다."]

    return ValidatedReview(
        comments=comments,
        summary=summary,
        event=event,
        positives=positives,
        concerns=concerns,
    )


def build_review_payload(
    summary: str,
    event: str,
    comments: list[ReviewComment],
    positives: list[str],
    concerns: list[str],
) -> dict[str, Any]:
    """GitHub Review API가 기대하는 본문/인라인 코멘트 구조를 만든다."""
    positive_items = positives or list(DEFAULT_FALLBACK_POSITIVES)
    concern_items = concerns or [DEFAULT_NO_CONCERNS_TEXT]
    body_lines = [
        normalize_text(summary) or DEFAULT_NO_FINDINGS_SUMMARY,
        "",
        "### 좋은 점",
    ]
    body_lines.extend(f"- {item}" for item in positive_items)
    body_lines.extend(
        [
            "",
            "### 개선이 필요한 점",
        ]
    )
    body_lines.extend(f"- {item}" for item in concern_items)
    body_lines.extend(
        [
            "",
            "### 라인 단위 코멘트",
        ]
    )

    if comments:
        body_lines.append(f"- 자동 리뷰에서 {len(comments)}개의 라인 단위 개선 사항을 남겼습니다.")
    else:
        body_lines.append("- 라인 단위로 남길 개선 사항은 발견되지 않았습니다.")

    return {
        "body": "\n".join(body_lines),
        "event": event,
        "comments": [
            {
                "path": comment.path,
                "line": comment.line,
                "side": comment.side,
                "body": comment.body,
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
    """로그와 후속 처리에서 재사용할 리뷰 결과 요약을 만든다."""
    return {
        "status": "completed",
        "repository": repository,
        "pull_number": pull_number,
        "summary": validated_review.summary,
        "event": validated_review.event,
        "comment_count": len(validated_review.comments),
        "positive_count": len(validated_review.positives),
        "concern_count": len(validated_review.concerns),
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


def load_patchable_pr_files(github: GitHubApi, pull_number: int, *, log_prefix: str = "") -> list[PullRequestFile]:
    log_progress(log_prefix, f"Fetching PR files for {github.repository}#{pull_number}")
    raw_files = github.list_pr_files(pull_number)
    pr_files = build_pr_files(raw_files)
    log_progress(log_prefix, f"Loaded {len(pr_files)} patchable file(s)")
    return pr_files


def generate_review_artifacts(
    repository: str,
    pull_number: int,
    pr_files: list[PullRequestFile],
    *,
    log_prefix: str = "",
) -> ReviewGenerationArtifacts:
    prompt = make_prompt(repository, pull_number, pr_files)
    if os.environ.get("WRITE_PROMPT_DEBUG") == "1":
        write_prompt_debug_file(prompt)

    mlx_started_at = time.monotonic()
    log_progress(log_prefix, "Running MLX review model")
    mlx_result = run_mlx(prompt, log_prefix=log_prefix)
    log_progress(log_prefix, f"MLX review completed in {time.monotonic() - mlx_started_at:.1f}s")
    validated_review = validate_mlx_output(mlx_result, pr_files, log_prefix=log_prefix)
    payload = build_review_payload(
        validated_review.summary,
        validated_review.event,
        validated_review.comments,
        validated_review.positives,
        validated_review.concerns,
    )
    return ReviewGenerationArtifacts(
        prompt=prompt,
        mlx_result=mlx_result,
        validated_review=validated_review,
        payload=payload,
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
    pr_files = load_patchable_pr_files(github, pull_number, log_prefix=log_prefix)

    if not pr_files:
        return {
            "status": "skipped",
            "reason": "No patchable files found.",
            "repository": repository,
            "pull_number": pull_number,
        }

    artifacts = generate_review_artifacts(repository, pull_number, pr_files, log_prefix=log_prefix)
    result = build_review_result(
        repository,
        pull_number,
        artifacts.validated_review,
        artifacts.payload,
        auth_source,
    )

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
