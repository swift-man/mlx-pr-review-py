#!/usr/bin/env python3
"""Deterministic review client for end-to-end webhook testing."""

from __future__ import annotations

import json
import sys
from typing import Any


def choose_comment_target(payload: dict[str, Any]) -> tuple[str, int]:
    for file_info in payload.get("files", []):
        path = str(file_info.get("path") or "").strip()
        valid_lines = file_info.get("valid_comment_lines") or []
        if not path or not isinstance(valid_lines, list):
            continue

        for raw_line in valid_lines:
            try:
                line = int(raw_line)
            except (TypeError, ValueError):
                continue
            return path, line

    raise RuntimeError("No valid comment target found in prompt payload")


def build_response(payload: dict[str, Any]) -> dict[str, Any]:
    path, line = choose_comment_target(payload)
    # Phase 2 스키마 (must_fix / suggestions / comments[].severity) 로 응답해,
    # E2E 웹훅 테스트가 구 스키마의 legacy_concerns 호환 경로에만 의존하지 않도록 한다.
    # event 는 should_request_changes 로직에 의해 자동 결정되므로 굳이 REQUEST_CHANGES 를
    # 강제하지 않고, must_fix 가 비어 있어 COMMENT 로 남게 두어 반복 테스트 시 PR 이
    # 차단 상태로 쌓이지 않게 한다.
    return {
        "summary": (
            "테스트용 자동 리뷰입니다. PR 본문 코멘트와 라인별 코멘트가 모두 정상적으로 등록되는지 확인합니다."
        ),
        "event": "COMMENT",
        "positives": [
            "응답 스키마와 GitHub 리뷰 등록 경로를 한 번에 검증할 수 있도록 테스트 흐름이 단순하게 구성되어 있습니다.",
        ],
        "must_fix": [],
        "suggestions": [
            "아래 라인 코멘트가 실제 PR diff에 정상적으로 표시되는지 함께 확인해 주세요.",
        ],
        "comments": [
            {
                "path": path,
                "line": line,
                "severity": "Minor",
                "body": (
                    "테스트용 라인 코멘트입니다. 이 메시지가 PR 상세 diff에 보이면 웹훅과 Review API 연동이 정상입니다."
                ),
            }
        ],
    }


def main() -> int:
    payload = json.load(sys.stdin)
    result = build_response(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
