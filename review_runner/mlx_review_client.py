#!/usr/bin/env python3
"""Run PR review generation with MLX + mlx-lm and return strict JSON."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import threading
from typing import Any


DEFAULT_MODEL = "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_MAX_FINDINGS = 10
DEFAULT_SUMMARY = "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
DEFAULT_POSITIVES = [
    "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
]
MAX_PARSE_ERROR_SNIPPET = 2000
MAX_SALVAGE_ITEMS = 5

TRAILING_COMMA_RE = re.compile(r",(?=\s*[}\]])")
BARE_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)')
UNQUOTED_EVENT_RE = re.compile(r'("event"\s*:\s*)(COMMENT|REQUEST_CHANGES)(\s*[,}])')
SUMMARY_STOP_RE = re.compile(
    r'(?i)(?:\bpositive(?:s)?\d*\s*:|["\']?positives["\']?\s*:|\bconcern(?:s)?\d*\s*:|["\']?concerns["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:)'
)
GENERIC_FIELD_STOP_RE = re.compile(
    r'(?i)(?:["\']?summary["\']?\s*:|["\']?event["\']?\s*:|["\']?positives["\']?\s*:|["\']?concerns["\']?\s*:|["\']?comments["\']?\s*:|\bpositive(?:s)?\d*\s*:|\bconcern(?:s)?\d*\s*:)'
)

POSITIVE_ITEM_RE = re.compile(
    r'(?is)\bpositive(?:s)?\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
CONCERN_ITEM_RE = re.compile(
    r'(?is)\bconcern(?:s)?\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
SMART_QUOTES_TRANSLATION = str.maketrans(
    {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
    }
)
SECTION_HEADER_RE = re.compile(r"(?im)^\s*(positives|concerns|comments|event|response_schema)\s*:\s*$")
MARKDOWN_ITEM_RE = re.compile(r"(?m)^\s*-\s+(.+?)\s*$")
PROMPT_ECHO_MARKERS = (
    "review_runner/review_service.py",
    "review_runner/",
    "valid_comment_lines",
    "RIGHT-side",
    "response_schema",
    "style-only",
    "praise-only",
    "TRAILING_COMMA_RE",
    "SUMMARY_STOP_RE",
    "GENERIC_FIELD_STOP_RE",
    "POSITIVE_ITEM_RE",
    "CONCERN_ITEM_RE",
)

_MODEL = None
_TOKENIZER = None
_LOAD_LOCK = threading.Lock()


def get_env_bool(name: str, default: bool = False) -> bool:
    """불리언 환경변수는 여러 truthy 문자열을 허용한다."""
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def get_env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def get_env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float") from exc


def get_model_name() -> str:
    return os.environ.get("MLX_MODEL", DEFAULT_MODEL)


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


def load_runtime() -> tuple[Any, Any]:
    """모델과 토크나이저를 한 번만 로드해 웹훅 요청 사이에서 재사용한다."""
    try:
        from mlx_lm import load
    except ImportError as exc:
        raise RuntimeError(
            "mlx-lm is not installed. Install the review venv with `pip install -r review_runner/requirements.txt`."
        ) from exc

    global _MODEL, _TOKENIZER
    if _MODEL is not None and _TOKENIZER is not None:
        return _MODEL, _TOKENIZER

    with _LOAD_LOCK:
        if _MODEL is not None and _TOKENIZER is not None:
            return _MODEL, _TOKENIZER

        tokenizer_config = {
            # Fail fast for models that require remote code instead of prompting in a webhook process.
            "trust_remote_code": get_env_bool("MLX_TRUST_REMOTE_CODE", default=False),
        }
        _MODEL, _TOKENIZER = load(get_model_name(), tokenizer_config=tokenizer_config)
        return _MODEL, _TOKENIZER


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    """모델이 엄격한 JSON 계약을 따르도록 시스템/사용자 메시지를 만든다."""
    max_findings = get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS)
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [
        {
            "role": "system",
            "content": (
                "You are a senior software engineer performing pull request review. "
                "This task has a strict output contract: respond with exactly one JSON object written for a Korean-speaking reviewer. "
                "Return exactly one JSON object and nothing else. "
                "Never wrap the answer in markdown fences. "
                "The response body must be valid JSON, and every natural-language string value must be written in Korean. "
                'The only allowed non-Korean values are the event enum values "COMMENT" and "REQUEST_CHANGES", plus file paths, symbols, and API names when translation would be incorrect. '
                "Report only high-confidence issues that are directly visible in the diff. "
                "Use strict JSON syntax with double-quoted keys and string values. "
                "Do not use trailing commas, single quotes, comments, or unquoted enum values. "
                "All natural-language output must be written in Korean. This is mandatory and overrides any conflicting habit from the model. "
                "Write summary, positives, concerns, and every line comment body in Korean only. "
                "Use only these top-level keys: summary, event, positives, concerns, comments. "
                "positives and concerns must be JSON arrays, never inline labels such as positive1: or concerns1:. "
                "Do not put positives, concerns, or comments inside the summary string. "
                "Do not use English sentences in JSON values unless a file path, symbol, or API name requires it. "
                "Before deciding there are no issues, explicitly check whether the diff disables validation, bypasses authentication, skips a security check, logs a token/secret, or turns an error path into a success path. "
                "Also check for typos in public response keys, payload fields, and GitHub header names because those break integrations even when the code still looks simple. "
                "If any of those patterns appear in added lines, you must add at least one concern and one line comment, and set event to REQUEST_CHANGES. "
                "Do not answer with generic praise such as 'PR diff가 잘 작성되었습니다' or '잘 정리되어 있습니다' unless it is tied to a specific strength visible in the diff. "
                "Do not say there are no improvements needed when the diff removes a guard, returns early from a validation branch, or prints a secret value. "
                "Do not flag an MLX_MODEL value change by itself unless the diff also shows a concrete compatibility, availability, memory, or rollout risk. "
                f"Return at most {max_findings} findings. "
                "Do not write praise-only line comments. "
                'Follow this shape exactly: {"summary":"한국어 요약","event":"COMMENT","positives":["한국어 장점"],"concerns":["한국어 개선점"],"comments":[{"path":"file.py","line":12,"body":"한국어 라인 코멘트"}]}. '
                'If there are no actionable issues, return {"summary":"...","event":"COMMENT","positives":["..."],"concerns":[],"comments":[]} '
                "and use summary plus positives to briefly mention what looks strong about the diff in Korean."
            ),
        },
        {
            "role": "user",
            "content": (
                "Review this pull request diff payload and respond using the response_schema inside it. "
                "반드시 JSON 객체 하나만 반환하세요. "
                "summary, positives, concerns, comments[].body 의 모든 자연어 문장은 한국어로만 작성하세요. "
                "event 값만 COMMENT 또는 REQUEST_CHANGES 를 사용할 수 있습니다. "
                "추가된 코드에서 검증 우회, 인증/서명 체크 제거, 민감정보 로그 출력, 예외 대신 성공 반환이 보이면 반드시 지적하세요. "
                "특히 signature 검증을 건너뛰는 return, token/secret 출력은 높은 우선순위 이슈로 취급하세요. "
                "공개 응답 키 이름이나 GitHub 헤더 이름의 오타처럼 기본 계약을 깨는 변경도 반드시 지적하세요. "
                "단순히 MLX_MODEL 값이 바뀌었다는 이유만으로는 코멘트하지 말고, 실제 호환성/가용성/메모리 위험이 diff에 보일 때만 지적하세요. "
                "영문 diff 메타데이터를 그대로 복사하지 말고, 한국어 리뷰 문장으로 정리하세요.\n"
                f"{compact_payload}"
            ),
        },
    ]


def render_prompt(tokenizer: Any, messages: list[dict[str, str]]) -> str:
    """모델별 chat template 유무를 흡수해 최종 프롬프트 문자열을 만든다."""
    apply_chat_template = getattr(tokenizer, "apply_chat_template", None)
    if apply_chat_template is None:
        return "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages)

    try:
        rendered = apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    except TypeError:
        rendered = apply_chat_template(messages, add_generation_prompt=True)

    if isinstance(rendered, str):
        return rendered

    decode = getattr(tokenizer, "decode", None)
    if decode is None:
        raise RuntimeError("Tokenizer returned token ids without a decode() method")
    return decode(rendered)


def run_generation(prompt: str) -> str:
    """MLX 모델을 실제로 실행하고 원시 텍스트 응답을 받는다."""
    try:
        from mlx_lm import generate
    except ImportError as exc:
        raise RuntimeError(
            "mlx-lm is not installed. Install the review venv with `pip install -r review_runner/requirements.txt`."
        ) from exc

    model, tokenizer = load_runtime()
    generation_kwargs: dict[str, Any] = {
        "prompt": prompt,
        "max_tokens": get_env_int("MLX_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        "verbose": False,
        "temp": get_env_float("MLX_TEMPERATURE", 0.0),
        "top_p": get_env_float("MLX_TOP_P", 1.0),
    }

    repetition_penalty = os.environ.get("MLX_REPETITION_PENALTY")
    if repetition_penalty is not None:
        generation_kwargs["repetition_penalty"] = get_env_float("MLX_REPETITION_PENALTY", 1.0)

    repetition_context_size = os.environ.get("MLX_REPETITION_CONTEXT_SIZE")
    if repetition_context_size is not None:
        generation_kwargs["repetition_context_size"] = get_env_int("MLX_REPETITION_CONTEXT_SIZE", 128)

    max_kv_size = os.environ.get("MLX_MAX_KV_SIZE")
    if max_kv_size is not None:
        generation_kwargs["max_kv_size"] = get_env_int("MLX_MAX_KV_SIZE", 0)

    try:
        return generate(model, tokenizer, **generation_kwargs)
    except TypeError:
        # Fallback for older mlx-lm versions with a smaller generate() surface.
        return generate(
            model,
            tokenizer,
            prompt=prompt,
            max_tokens=generation_kwargs["max_tokens"],
            verbose=False,
        )


def strip_markdown_fences(text: str) -> str:
    """모델이 실수로 markdown fence를 둘러도 내부 JSON만 추출한다."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def extract_json_object(text: str) -> str:
    """자유 형식 출력에서 첫 번째 완전한 JSON 객체를 잘라낸다."""
    candidate = strip_markdown_fences(text)
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    if start < 0:
        raise RuntimeError(f"Model output did not contain a JSON object:\n{candidate}")

    depth = 0
    string_delimiter: str | None = None
    escape = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if string_delimiter is not None:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == string_delimiter:
                string_delimiter = None
            continue

        if char in {'"', "'"}:
            string_delimiter = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : index + 1]

    raise RuntimeError(f"Could not extract a complete JSON object from model output:\n{candidate}")


def format_error_snippet(text: str, limit: int = MAX_PARSE_ERROR_SNIPPET) -> str:
    snippet = text.strip()
    if len(snippet) <= limit:
        return snippet
    return f"{snippet[:limit].rstrip()}\n... [truncated]"


def repair_json_candidate(candidate: str) -> str:
    """자주 나오는 깨진 JSON 패턴을 최소한으로 보정한다."""
    repaired = candidate.translate(SMART_QUOTES_TRANSLATION)
    repaired = TRAILING_COMMA_RE.sub("", repaired)
    repaired = BARE_KEY_RE.sub(r'\1"\2"\3', repaired)
    repaired = UNQUOTED_EVENT_RE.sub(r'\1"\2"\3', repaired)
    return repaired


def find_key_value_start(text: str, key: str) -> int:
    """느슨한 키 표기까지 허용해 특정 필드 값의 시작 위치를 찾는다."""
    pattern = re.compile(rf'(?i)["\']?{re.escape(key)}["\']?\s*:')
    match = pattern.search(text)
    if match is None:
        return -1
    return match.end()


def scan_balanced_segment(text: str, start: int, open_char: str, close_char: str) -> str | None:
    """문자열 리터럴을 건너뛰면서 균형 잡힌 배열/객체 조각을 찾는다."""
    if start < 0 or start >= len(text) or text[start] != open_char:
        return None

    depth = 0
    string_delimiter: str | None = None
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if string_delimiter is not None:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == string_delimiter:
                string_delimiter = None
            continue

        if char in {'"', "'"}:
            string_delimiter = char
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]

    return None


def parse_json_fragment(fragment: str) -> Any:
    """엄격한 JSON 파싱과 느슨한 literal 파싱을 순서대로 시도한다."""
    for candidate in (fragment, repair_json_candidate(fragment)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

        try:
            return ast.literal_eval(candidate)
        except (SyntaxError, ValueError):
            pass

    return None


def extract_array_field(text: str, key: str) -> list[Any] | None:
    """깨진 응답에서도 배열 필드를 최대한 복구한다."""
    value_start = find_key_value_start(text, key)
    if value_start < 0:
        return None

    while value_start < len(text) and text[value_start].isspace():
        value_start += 1

    if value_start >= len(text):
        return None

    if text[value_start] != "[":
        array_start = text.find("[", value_start)
        if array_start < 0:
            return None
        value_start = array_start

    fragment = scan_balanced_segment(text, value_start, "[", "]")
    if fragment is None:
        return None

    parsed = parse_json_fragment(fragment)
    if isinstance(parsed, list):
        return parsed
    return None


def extract_string_field(text: str, key: str, stop_pattern: re.Pattern[str]) -> str:
    """다음 필드 경계 전까지를 잘라 문자열 필드처럼 복원한다."""
    value_start = find_key_value_start(text, key)
    if value_start < 0:
        return ""

    while value_start < len(text) and text[value_start].isspace():
        value_start += 1

    if value_start >= len(text):
        return ""

    remainder = text[value_start:]
    if remainder.startswith(('"', "'")):
        remainder = remainder[1:]

    stop_match = stop_pattern.search(remainder)
    field_text = remainder[: stop_match.start()] if stop_match is not None else remainder
    return normalize_text(field_text.strip().strip('"\',]}'))


def extract_labeled_items(text: str, item_pattern: re.Pattern[str]) -> list[str]:
    return normalize_text_list([match.group(1) for match in item_pattern.finditer(text)], max_items=10)


def looks_like_prompt_echo(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(marker in normalized for marker in PROMPT_ECHO_MARKERS):
        return True
    return False


def sanitize_summary(summary: str) -> str:
    normalized = normalize_text(summary)
    if not normalized or looks_like_prompt_echo(normalized):
        return DEFAULT_SUMMARY
    return normalized


def sanitize_items(items: list[str], max_items: int = MAX_SALVAGE_ITEMS) -> list[str]:
    """복구된 목록에서 중복과 프롬프트 echo를 걷어낸다."""
    sanitized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = normalize_text(item)
        if text.startswith("- "):
            text = text[2:].strip()
        if not text or text in seen or looks_like_prompt_echo(text):
            continue
        seen.add(text)
        sanitized.append(text)
        if len(sanitized) >= max_items:
            break
    return sanitized


def extract_markdown_section_items(text: str, section_name: str) -> list[str]:
    """markdown 섹션 형태로 답한 경우 목록 항목만 추린다."""
    section_pattern = re.compile(rf"(?ims)^\s*{re.escape(section_name)}\s*:\s*$")
    match = section_pattern.search(text)
    if match is None:
        return []

    section_start = match.end()
    next_match = SECTION_HEADER_RE.search(text, section_start)
    section_body = text[section_start : next_match.start()] if next_match is not None else text[section_start:]
    return [match.group(1) for match in MARKDOWN_ITEM_RE.finditer(section_body)]


def extract_markdown_event(text: str) -> str:
    """markdown 섹션의 event 값을 첫 줄 기준으로 복구한다."""
    section_pattern = re.compile(r"(?ims)^\s*event\s*:\s*$")
    match = section_pattern.search(text)
    if match is None:
        return ""

    section_start = match.end()
    next_match = SECTION_HEADER_RE.search(text, section_start)
    section_body = text[section_start : next_match.start()] if next_match is not None else text[section_start:]
    first_line = normalize_text(section_body.splitlines()[0] if section_body.splitlines() else "")
    if first_line.startswith("- "):
        first_line = first_line[2:].strip()
    return first_line


def extract_freeform_summary(text: str) -> str:
    """구조화된 섹션 앞의 자유 텍스트를 summary 후보로 사용한다."""
    header_match = SECTION_HEADER_RE.search(text)
    head = text[: header_match.start()] if header_match is not None else text
    return normalize_text(head.strip().strip("{}"))


def fallback_response(raw_output: str) -> dict[str, Any]:
    """끝까지 복구하지 못해도 호출부가 계속 진행할 기본 응답을 만든다."""
    return {
        "summary": DEFAULT_SUMMARY,
        "event": "COMMENT",
        "positives": list(DEFAULT_POSITIVES),
        "concerns": [],
        "comments": [],
    }


def extract_section_items(text: str, key: str, item_pattern: re.Pattern[str]) -> list[str]:
    """배열, 라벨형 텍스트, markdown 섹션 순서로 목록 필드를 복구한다."""
    items = normalize_text_list(extract_array_field(text, key), max_items=10)
    if items:
        return items

    items = extract_labeled_items(text, item_pattern)
    if items:
        return items

    return normalize_text_list(extract_markdown_section_items(text, key), max_items=10)


def normalize_event_value(raw_event: str, *, has_comments: bool) -> str:
    """모델이 event를 어색하게 내도 최종 허용값으로 정규화한다."""
    event = raw_event.strip().upper()
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        return "REQUEST_CHANGES" if has_comments else "COMMENT"
    if not has_comments:
        return "COMMENT"
    return event


def salvage_broken_output(text: str) -> dict[str, Any] | None:
    """깨진 응답에서도 summary/positives/concerns/comments를 최대한 건져낸다."""
    summary = extract_string_field(text, "summary", SUMMARY_STOP_RE) or extract_freeform_summary(text)
    event = extract_string_field(text, "event", GENERIC_FIELD_STOP_RE).upper() or extract_markdown_event(text).upper()
    positives = extract_section_items(text, "positives", POSITIVE_ITEM_RE)
    concerns = extract_section_items(text, "concerns", CONCERN_ITEM_RE)
    comments_raw = extract_array_field(text, "comments") or []
    comments = [item for item in comments_raw if isinstance(item, dict)]

    summary = sanitize_summary(summary)
    positives = sanitize_items(positives)
    concerns = sanitize_items(concerns)
    event = normalize_event_value(event, has_comments=bool(comments))

    return {
        "summary": summary,
        "event": event,
        "positives": positives or list(DEFAULT_POSITIVES),
        "concerns": concerns,
        "comments": comments,
    }


def parse_model_json(raw_output: str) -> dict[str, Any]:
    """엄격 파싱과 복구 전략을 순서대로 적용해 모델 출력을 해석한다."""
    try:
        candidate = extract_json_object(raw_output)
    except RuntimeError:
        salvaged = salvage_broken_output(raw_output)
        if salvaged is not None:
            return salvaged
        raise

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as json_exc:
        repaired = repair_json_candidate(candidate)
        if repaired != candidate:
            try:
                parsed = json.loads(repaired)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(parsed, dict):
                    return parsed

        for fallback_candidate in (candidate, repaired):
            try:
                parsed = ast.literal_eval(fallback_candidate)
            except (SyntaxError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed

        salvaged = salvage_broken_output(candidate)
        if salvaged is not None:
            return salvaged

        raise RuntimeError(
            "Model returned invalid JSON-like output.\n"
            f"Extracted candidate:\n{format_error_snippet(candidate)}"
        ) from json_exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Model returned a non-object JSON value: {parsed!r}")

    return parsed


def normalize_comment(raw_comment: dict[str, Any]) -> dict[str, Any] | None:
    """리뷰 코멘트를 path/line/body가 모두 있는 최소 구조로 정규화한다."""
    path = str(raw_comment.get("path") or "").strip()
    body = normalize_text(raw_comment.get("body"))
    line = raw_comment.get("line")
    try:
        line_number = int(line)
    except (TypeError, ValueError):
        return None

    if not path or not body:
        return None

    return {"path": path, "line": line_number, "body": body}


def normalize_response(raw_response: dict[str, Any]) -> dict[str, Any]:
    """최종 응답 스키마를 보장하고 중복 코멘트를 제거한다."""
    max_findings = get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS)
    comments: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()

    for raw_comment in raw_response.get("comments", []):
        if not isinstance(raw_comment, dict):
            continue
        normalized = normalize_comment(raw_comment)
        if normalized is None:
            continue
        identity = (
            normalized["path"],
            normalized["line"],
            normalized["body"],
        )
        if identity in seen:
            continue
        seen.add(identity)
        comments.append(normalized)
        if len(comments) >= max_findings:
            break

    summary = normalize_text(raw_response.get("summary"))
    if not summary:
        summary = DEFAULT_SUMMARY

    positives = normalize_text_list(raw_response.get("positives"))
    concerns = normalize_text_list(raw_response.get("concerns"))
    event = normalize_event_value(str(raw_response.get("event") or ""), has_comments=bool(comments))

    return {
        "summary": summary,
        "event": event,
        "positives": positives,
        "concerns": concerns,
        "comments": comments,
    }


def review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """payload를 모델에 전달하고, 결과를 계약에 맞는 JSON으로 정리한다."""
    model, tokenizer = load_runtime()
    del model
    messages = build_messages(payload)
    prompt = render_prompt(tokenizer, messages)
    raw_output = run_generation(prompt)
    try:
        parsed = parse_model_json(raw_output)
    except RuntimeError as exc:
        parsed = fallback_response(raw_output)
    return normalize_response(parsed)


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점으로 warmup과 실제 리뷰 실행을 모두 담당한다."""
    parser = argparse.ArgumentParser(description="Run MLX-based PR review generation")
    parser.add_argument("--warmup", action="store_true", help="Load the configured MLX model and exit")
    args = parser.parse_args(argv)

    if args.warmup:
        load_runtime()
        print(json.dumps({"status": "ready", "model": get_model_name()}, ensure_ascii=False))
        return 0

    payload = json.load(sys.stdin)
    result = review_payload(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
