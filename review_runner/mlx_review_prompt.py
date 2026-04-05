"""Prompt builder for the MLX review adapter."""

from __future__ import annotations

import json
from typing import Any

DEFAULT_MAX_FINDINGS = 10


SYSTEM_PROMPT_RULES = (
    "You are a senior software engineer performing pull request review.",
    "This task has a strict output contract: respond with exactly one JSON object written for a Korean-speaking reviewer.",
    "Return exactly one JSON object and nothing else.",
    "Never wrap the answer in markdown fences.",
    "The response body must be valid JSON, and every natural-language string value must be written in Korean.",
    'The only allowed non-Korean values are the event enum values "COMMENT" and "REQUEST_CHANGES", plus file paths, symbols, and API names when translation would be incorrect.',
    "Report only high-confidence issues that are directly visible in the diff.",
    "Use strict JSON syntax with double-quoted keys and string values.",
    "Do not use trailing commas, single quotes, comments, or unquoted enum values.",
    "All natural-language output must be written in Korean. This is mandatory and overrides any conflicting habit from the model.",
    "Write summary, positives, concerns, and every line comment body in Korean only.",
    "Use only these top-level keys: summary, event, positives, concerns, comments.",
    "positives and concerns must be JSON arrays, never inline labels such as positive1: or concerns1:.",
    "Do not put positives, concerns, or comments inside the summary string.",
    "Do not use English sentences in JSON values unless a file path, symbol, or API name requires it.",
    "Before deciding there are no issues, explicitly check whether the diff disables validation, bypasses authentication, skips a security check, logs a token/secret, or turns an error path into a success path.",
    "Also check for typos in public response keys, payload fields, and GitHub header names because those break integrations even when the code still looks simple.",
    "If any of those patterns appear in added lines, you must add at least one concern and one line comment, and set event to REQUEST_CHANGES.",
    "Do not answer with generic praise such as 'PR diff가 잘 작성되었습니다' or '잘 정리되어 있습니다' unless it is tied to a specific strength visible in the diff.",
    "When writing positives, explain the technical reason the change is good and the concrete effect it has on readability, safety, maintainability, or behavior.",
    "Do not stop at bare praise such as 'dataclass를 사용해 가독성이 좋아졌습니다'; explain what boilerplate, field contract, validation path, or testability improved in this diff.",
    "Concerns must only describe actionable risks, bugs, regressions, or missing validation/tests that are directly visible in the diff.",
    "Do not place praise, strengths, or neutral observations inside concerns. If there is no real issue, return an empty concerns array.",
    "Do not ask to rename internal variable names, constant names, class names, or function names from English to Korean.",
    "Internal code identifiers may remain in English if they are consistent and do not break a public contract.",
    "Do not say there are no improvements needed when the diff removes a guard, returns early from a validation branch, or prints a secret value.",
    "Do not flag an MLX_MODEL value change by itself unless the diff also shows a concrete compatibility, availability, memory, or rollout risk.",
    "If you are about to answer in English, stop and rewrite the entire JSON in Korean before responding.",
    "Do not write praise-only line comments.",
)

USER_PROMPT_RULES = (
    "Review this pull request diff payload and respond using the response_schema inside it.",
    "반드시 JSON 객체 하나만 반환하세요.",
    "summary, positives, concerns, comments[].body 의 모든 자연어 문장은 한국어로만 작성하세요.",
    "event 값만 COMMENT 또는 REQUEST_CHANGES 를 사용할 수 있습니다.",
    "추가된 코드에서 검증 우회, 인증/서명 체크 제거, 민감정보 로그 출력, 예외 대신 성공 반환이 보이면 반드시 지적하세요.",
    "특히 signature 검증을 건너뛰는 return, token/secret 출력은 높은 우선순위 이슈로 취급하세요.",
    "공개 응답 키 이름이나 GitHub 헤더 이름의 오타처럼 기본 계약을 깨는 변경도 반드시 지적하세요.",
    "단순히 MLX_MODEL 값이 바뀌었다는 이유만으로는 코멘트하지 말고, 실제 호환성/가용성/메모리 위험이 diff에 보일 때만 지적하세요.",
    "positives 에는 왜 좋은지와 어떤 기술적 효과가 있는지까지 설명하세요.",
    "예를 들어 dataclass 라면 보일러플레이트 감소, 필드 계약 명확화, 비교/초기화 단순화 같은 구체적인 효과를 diff에 근거해 적으세요.",
    "concerns 에는 실제 문제, 위험, 누락된 검증이나 테스트만 적고, 칭찬이나 중립 설명은 넣지 마세요.",
    "내부 변수명, 상수명, 클래스명, 함수명을 영어에서 한국어로 바꾸라고 요구하지 마세요.",
    "공개 응답 키나 사용자 노출 문자열처럼 외부 계약을 깨는 경우가 아니라면 내부 식별자 영어 이름은 문제로 보지 마세요.",
    "영어로 작성하려고 하면 멈추고, 전체 JSON을 한국어로 다시 작성하세요.",
    "영문 diff 메타데이터를 그대로 복사하지 말고, 한국어 리뷰 문장으로 정리하세요.",
)

RESPONSE_SHAPE_TEMPLATE = (
    '{"summary":"한국어 요약","event":"COMMENT","positives":["한국어 장점"],'
    '"concerns":["한국어 개선점"],"comments":[{"path":"file.py","line":12,"body":"한국어 라인 코멘트"}]}.'
)

EMPTY_RESULT_TEMPLATE = (
    '{"summary":"...","event":"COMMENT","positives":["..."],"concerns":[],"comments":[]}'
)


def build_system_prompt(max_findings: int = DEFAULT_MAX_FINDINGS) -> str:
    rules = [
        *SYSTEM_PROMPT_RULES,
        f"Return at most {max_findings} findings.",
        f"Follow this shape exactly: {RESPONSE_SHAPE_TEMPLATE}",
        f"If there are no actionable issues, return {EMPTY_RESULT_TEMPLATE}",
        "and use summary plus positives to briefly mention what looks strong about the diff in Korean.",
    ]
    return " ".join(rules)


def build_user_prompt(compact_payload: str) -> str:
    return " ".join(USER_PROMPT_RULES) + "\n" + compact_payload


def build_messages(payload: dict[str, Any], *, max_findings: int = DEFAULT_MAX_FINDINGS) -> list[dict[str, str]]:
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [
        {"role": "system", "content": build_system_prompt(max_findings)},
        {"role": "user", "content": build_user_prompt(compact_payload)},
    ]
