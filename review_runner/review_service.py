#!/usr/bin/env python3
"""Shared PR review service used by CLI entrypoints and the webhook server."""

from __future__ import annotations

import json
import os
import shlex
import ssl
import subprocess
import textwrap
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_MLX_REVIEW_CMD = "python3 -m review_runner.mlx_review_client"
DEFAULT_CA_BUNDLE_ENV = "GITHUB_CA_BUNDLE"
DEFAULT_NO_FINDINGS_SUMMARY = (
    "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
)
DEFAULT_FALLBACK_POSITIVES = [
    "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
]
DEFAULT_NO_CONCERNS_TEXT = "이번 diff 기준으로 별도 개선 필요 사항은 발견되지 않았습니다."


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


def build_ssl_context() -> ssl.SSLContext:
    cafile = os.environ.get(DEFAULT_CA_BUNDLE_ENV) or os.environ.get("SSL_CERT_FILE") or certifi.where()
    return ssl.create_default_context(cafile=cafile)


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


class GitHubApi:
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

        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "mac-mini-pr-reviewer",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.ssl_context) as response:
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


def format_no_findings_summary(summary: str) -> str:
    normalized = normalize_text(summary)
    if not normalized or normalized in {
        "Automated review completed.",
        "Automated MLX review completed.",
        "No actionable issues found.",
        "자동 리뷰를 완료했습니다.",
        "자동 MLX 리뷰를 완료했습니다.",
        "검토할 만한 문제를 찾지 못했습니다.",
        "지적할 만한 문제는 보이지 않습니다.",
    }:
        return DEFAULT_NO_FINDINGS_SUMMARY

    return normalized


def make_prompt(repository: str, pull_number: int, files: list[PullRequestFile]) -> str:
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


def run_mlx(prompt: str) -> dict[str, Any]:
    raw_command = os.environ.get("MLX_REVIEW_CMD", DEFAULT_MLX_REVIEW_CMD)
    command = shlex.split(raw_command)
    completed = subprocess.run(
        command,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "MLX command failed with exit code "
            f"{completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        )

    stdout = completed.stdout.strip()
    if not stdout:
        raise RuntimeError("MLX command returned empty output")

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MLX command returned invalid JSON:\n{stdout}") from exc


def validate_mlx_output(
    result: dict[str, Any], files: list[PullRequestFile]
) -> tuple[list[ReviewComment], str, str, list[str], list[str]]:
    file_index = {f.filename: f for f in files}
    comments: list[ReviewComment] = []

    for raw in result.get("comments", []):
        path = raw.get("path")
        line = raw.get("line")
        body = normalize_text(raw.get("body"))
        if not path or not isinstance(line, int) or not body:
            continue

        pr_file = file_index.get(path)
        if pr_file is None or line not in pr_file.right_side_lines:
            continue

        comments.append(ReviewComment(path=path, line=line, body=body))

    summary = normalize_text(result.get("summary")) or "자동 리뷰를 완료했습니다."
    positives = normalize_text_list(result.get("positives"))
    concerns = normalize_text_list(result.get("concerns"))

    event = (result.get("event") or "").strip().upper()
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        event = "REQUEST_CHANGES" if comments else "COMMENT"

    if not comments:
        summary = format_no_findings_summary(summary)
        event = "COMMENT"
    if not positives:
        positives = list(DEFAULT_FALLBACK_POSITIVES)
    if comments and not concerns:
        concerns = summarize_comment_bodies(comments)

    return comments, summary, event, positives, concerns


def build_review_payload(
    summary: str,
    event: str,
    comments: list[ReviewComment],
    positives: list[str],
    concerns: list[str],
) -> dict[str, Any]:
    positive_items = positives or list(DEFAULT_FALLBACK_POSITIVES)
    concern_items = concerns or [DEFAULT_NO_CONCERNS_TEXT]
    body = "\n".join(
        [
            normalize_text(summary) or DEFAULT_NO_FINDINGS_SUMMARY,
            "",
            "### 좋은 점",
            *(f"- {item}" for item in positive_items),
            "",
            "### 개선이 필요한 점",
            *(f"- {item}" for item in concern_items),
        ]
    )

    return {
        "body": body,
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


def review_pull_request(
    repository: str,
    pull_number: int,
    token: str,
    api_url: str = DEFAULT_API_URL,
    dry_run: bool = False,
) -> dict[str, Any]:
    github = GitHubApi(token=token, repository=repository, api_url=api_url)
    raw_files = github.list_pr_files(pull_number)
    pr_files = build_pr_files(raw_files)

    if not pr_files:
        return {
            "status": "skipped",
            "reason": "No patchable files found.",
            "repository": repository,
            "pull_number": pull_number,
        }

    prompt = make_prompt(repository, pull_number, pr_files)
    if os.environ.get("WRITE_PROMPT_DEBUG") == "1":
        debug_path = os.environ.get("PROMPT_DEBUG_PATH", "/tmp/mlx_pr_review_prompt.json")
        with open(debug_path, "w", encoding="utf-8") as fh:
            fh.write(prompt)

    mlx_result = run_mlx(prompt)
    comments, summary, event, positives, concerns = validate_mlx_output(mlx_result, pr_files)
    payload = build_review_payload(summary, event, comments, positives, concerns)

    result = {
        "status": "completed",
        "repository": repository,
        "pull_number": pull_number,
        "event": event,
        "comment_count": len(comments),
        "payload": payload,
    }

    if dry_run:
        return result

    response = github.post_review(pull_number, payload)
    result["review_id"] = response.get("id")
    result["message"] = textwrap.dedent(
        f"""\
        Posted review successfully.
        Review ID: {response.get('id')}
        Event: {event}
        Comments: {len(comments)}
        """
    ).strip()
    return result
