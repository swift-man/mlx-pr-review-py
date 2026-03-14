#!/usr/bin/env python3
"""CLI entrypoint for reviewing one pull request from a GitHub event payload."""

from __future__ import annotations

import os
import sys
import json

from review_runner.review_service import DEFAULT_API_URL, resolve_github_token, review_pull_request


def is_dry_run() -> bool:
    """CLI와 웹훅 서버가 같은 기준으로 dry-run 여부를 읽도록 맞춘다."""
    return os.environ.get("DRY_RUN") == "1"


def parse_event() -> tuple[str, int]:
    """GitHub Actions 이벤트 파일에서 저장소와 PR 번호를 꺼낸다."""
    event_path = os.environ["GITHUB_EVENT_PATH"]
    repository = os.environ.get("GITHUB_REPOSITORY")
    with open(event_path, "r", encoding="utf-8") as fh:
        event = json.load(fh)

    repository = repository or event["repository"]["full_name"]
    pull_number = int(event["pull_request"]["number"])
    return repository, pull_number


def main() -> int:
    """단일 PR 리뷰 CLI 실행 흐름을 담당한다."""
    repository, pull_number = parse_event()
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL)
    auth = resolve_github_token(repository=repository, api_url=api_url)
    result = review_pull_request(
        repository=repository,
        pull_number=pull_number,
        token=auth.token,
        api_url=api_url,
        dry_run=is_dry_run(),
        auth_source=auth.source,
    )
    if is_dry_run():
        print(json.dumps(result.get("payload", result), ensure_ascii=False, indent=2))
        return 0

    print(result.get("message", json.dumps(result, ensure_ascii=False)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
