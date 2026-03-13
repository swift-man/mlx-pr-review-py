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
    return {
        "summary": (
            "테스트용 자동 리뷰입니다. PR 본문 코멘트와 라인별 코멘트가 모두 정상적으로 등록되는지 확인합니다."
        ),
        "event": "REQUEST_CHANGES",
        "comments": [
            {
                "path": path,
                "line": line,
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
