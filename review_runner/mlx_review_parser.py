"""Parser and normalizer for MLX review model output."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


DEFAULT_SUMMARY = "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
DEFAULT_POSITIVES = [
    "변경 범위가 비교적 집중되어 있어 의도를 따라가기 쉽습니다.",
]
DEFAULT_PARSE_ERROR_SNIPPET = 2000
DEFAULT_MAX_FINDINGS = 10
MAX_SALVAGE_ITEMS = 5

TRAILING_COMMA_RE = re.compile(r",(?=\s*[}\]])")
BARE_KEY_RE = re.compile(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)')
UNQUOTED_EVENT_RE = re.compile(r'("event"\s*:\s*)(COMMENT|REQUEST_CHANGES)(\s*[,}])')
SUMMARY_STOP_RE = re.compile(
    r'(?i)(?:\bpositive(?:s)?\d*\s*:|["\']?positives["\']?\s*:|\bconcern(?:s)?\d*\s*:|["\']?concerns["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:)'
)
GENERIC_FIELD_STOP_RE = re.compile(
    r'(?i)(?:["\']?summary["\']?\s*:|["\']?event["\']?\s*:|["\']?positives["\']?\s*:|["\']?concerns["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|\bpositive(?:s)?\d*\s*:|\bconcern(?:s)?\d*\s*:)'
)
POSITIVE_ITEM_RE = re.compile(
    r'(?is)\bpositive(?:s)?\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
CONCERN_ITEM_RE = re.compile(
    r'(?is)\bconcern(?:s)?\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
MUST_FIX_ITEM_RE = re.compile(
    r'(?is)\bmust_fix\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
SUGGESTION_ITEM_RE = re.compile(
    r'(?is)\bsuggestion(?:s)?\d*\s*:\s*(.+?)(?=(?:["\']?positive(?:s)?\d*["\']?\s*:|["\']?concern(?:s)?\d*["\']?\s*:|["\']?must_fix["\']?\s*:|["\']?suggestions["\']?\s*:|["\']?comments["\']?\s*:|["\']?event["\']?\s*:|$))'
)
SMART_QUOTES_TRANSLATION = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})
SECTION_HEADER_RE = re.compile(
    r"(?im)^\s*(positives|concerns|must_fix|suggestions|comments|event|response_schema)\s*:\s*$"
)
MARKDOWN_ITEM_RE = re.compile(r"(?m)^\s*-\s+(.+?)\s*$")
HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
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


def contains_hangul(text: str) -> bool:
    return bool(HANGUL_RE.search(text))


def strip_markdown_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def extract_json_object(text: str) -> str:
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


def format_error_snippet(text: str, limit: int = DEFAULT_PARSE_ERROR_SNIPPET) -> str:
    snippet = text.strip()
    if len(snippet) <= limit:
        return snippet
    return f"{snippet[:limit].rstrip()}\n... [truncated]"


def repair_json_candidate(candidate: str) -> str:
    repaired = candidate.translate(SMART_QUOTES_TRANSLATION)
    repaired = TRAILING_COMMA_RE.sub("", repaired)
    repaired = BARE_KEY_RE.sub(r'\1"\2"\3', repaired)
    repaired = UNQUOTED_EVENT_RE.sub(r'\1"\2"\3', repaired)
    return repaired


def find_key_value_start(text: str, key: str) -> int:
    pattern = re.compile(rf'(?i)["\']?{re.escape(key)}["\']?\s*:')
    match = pattern.search(text)
    if match is None:
        return -1
    return match.end()


def scan_balanced_segment(text: str, start: int, open_char: str, close_char: str) -> str | None:
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
    return any(marker in normalized for marker in PROMPT_ECHO_MARKERS)


def looks_like_non_korean_review_text(text: str) -> bool:
    normalized = normalize_text(text)
    if not normalized or contains_hangul(normalized):
        return False
    return bool(LATIN_RE.search(normalized))


def sanitize_korean_text(text: Any, fallback: str = "") -> str:
    normalized = normalize_text(text)
    if not normalized or looks_like_prompt_echo(normalized) or looks_like_non_korean_review_text(normalized):
        return fallback
    return normalized


def sanitize_summary(summary: str) -> str:
    return sanitize_korean_text(summary, DEFAULT_SUMMARY)


def sanitize_items(items: list[str], max_items: int = MAX_SALVAGE_ITEMS) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = sanitize_korean_text(item)
        if text.startswith("- "):
            text = text[2:].strip()
        if not text or text in seen:
            continue
        seen.add(text)
        sanitized.append(text)
        if len(sanitized) >= max_items:
            break
    return sanitized


def extract_markdown_section_items(text: str, section_name: str) -> list[str]:
    section_pattern = re.compile(rf"(?ims)^\s*{re.escape(section_name)}\s*:\s*$")
    match = section_pattern.search(text)
    if match is None:
        return []

    section_start = match.end()
    next_match = SECTION_HEADER_RE.search(text, section_start)
    section_body = text[section_start : next_match.start()] if next_match is not None else text[section_start:]
    return [match.group(1) for match in MARKDOWN_ITEM_RE.finditer(section_body)]


def extract_markdown_event(text: str) -> str:
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
    header_match = SECTION_HEADER_RE.search(text)
    head = text[: header_match.start()] if header_match is not None else text
    return normalize_text(head.strip().strip("{}"))


def fallback_response() -> dict[str, Any]:
    # 파싱이 완전히 실패했을 때 GitHub 리뷰가 비어 보이지 않도록 최소 구조만 채운다.
    # must_fix 와 suggestions 는 기본적으로 비어 있고 legacy_concerns 도 비워 둔다.
    return {
        "summary": DEFAULT_SUMMARY,
        "event": "COMMENT",
        "positives": list(DEFAULT_POSITIVES),
        "must_fix": [],
        "suggestions": [],
        "legacy_concerns": [],
        "comments": [],
    }


def extract_section_items(text: str, key: str, item_pattern: re.Pattern[str]) -> list[str]:
    items = normalize_text_list(extract_array_field(text, key), max_items=10)
    if items:
        return items

    items = extract_labeled_items(text, item_pattern)
    if items:
        return items

    return normalize_text_list(extract_markdown_section_items(text, key), max_items=10)


def normalize_event_value(raw_event: str, *, has_comments: bool) -> str:
    event = raw_event.strip().upper()
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        return "REQUEST_CHANGES" if has_comments else "COMMENT"
    if not has_comments:
        return "COMMENT"
    return event


def salvage_broken_output(text: str) -> dict[str, Any] | None:
    raw_summary = extract_string_field(text, "summary", SUMMARY_STOP_RE) or extract_freeform_summary(text)
    raw_event = extract_string_field(text, "event", GENERIC_FIELD_STOP_RE).upper() or extract_markdown_event(text).upper()
    positives = extract_section_items(text, "positives", POSITIVE_ITEM_RE)
    must_fix = extract_section_items(text, "must_fix", MUST_FIX_ITEM_RE)
    suggestions = extract_section_items(text, "suggestions", SUGGESTION_ITEM_RE)
    # 구 스키마(concerns 단일 필드)로 응답한 모델 출력도 살려낸다. 서비스 계층에서
    # risk marker 여부로 must_fix / suggestions 로 나눠 흡수한다.
    legacy_concerns = extract_section_items(text, "concerns", CONCERN_ITEM_RE)
    comments_raw = extract_array_field(text, "comments") or []
    comments = [item for item in comments_raw if isinstance(item, dict)]

    summary = sanitize_summary(raw_summary)
    positives = sanitize_items(positives)
    must_fix = sanitize_items(must_fix)
    suggestions = sanitize_items(suggestions)
    legacy_concerns = sanitize_items(legacy_concerns)
    event = normalize_event_value(raw_event, has_comments=bool(comments))

    has_meaningful_signal = (
        bool(sanitize_korean_text(raw_summary))
        or bool(positives)
        or bool(must_fix)
        or bool(suggestions)
        or bool(legacy_concerns)
        or bool(comments)
    )
    if not has_meaningful_signal:
        return None

    return {
        "summary": summary,
        "event": event,
        "positives": positives or list(DEFAULT_POSITIVES),
        "must_fix": must_fix,
        "suggestions": suggestions,
        "legacy_concerns": legacy_concerns,
        "comments": comments,
    }


def parse_model_json(raw_output: str) -> tuple[dict[str, Any], dict[str, str]]:
    try:
        candidate = extract_json_object(raw_output)
    except RuntimeError as exc:
        salvaged = salvage_broken_output(raw_output)
        if salvaged is not None:
            return salvaged, {
                "parse_mode": "salvaged_output",
                "parse_error": normalize_text(str(exc)),
            }
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
                    return parsed, {
                        "parse_mode": "repaired_json",
                        "parse_error": normalize_text(str(json_exc)),
                    }

        for fallback_candidate in (candidate, repaired):
            try:
                parsed = ast.literal_eval(fallback_candidate)
            except (SyntaxError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed, {
                    "parse_mode": "literal_eval",
                    "parse_error": normalize_text(str(json_exc)),
                }

        salvaged = salvage_broken_output(candidate)
        if salvaged is not None:
            return salvaged, {
                "parse_mode": "salvaged_candidate",
                "parse_error": normalize_text(str(json_exc)),
            }

        raise RuntimeError(
            "Model returned invalid JSON-like output.\n"
            f"Extracted candidate:\n{format_error_snippet(candidate)}"
        ) from json_exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"Model returned a non-object JSON value: {parsed!r}")

    return parsed, {"parse_mode": "strict_json", "parse_error": ""}


def increment_reason(counter: dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def normalize_comment(raw_comment: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    path = str(raw_comment.get("path") or "").strip()
    if not path:
        return None, "missing_path"

    body = sanitize_korean_text(raw_comment.get("body"))
    if not body:
        return None, "invalid_body"

    line = raw_comment.get("line")
    try:
        line_number = int(line)
    except (TypeError, ValueError):
        return None, "invalid_line"

    # severity 는 review_service.normalize_severity 가 정규화하므로 여기서는 원본 값을
    # 그대로 흘려보낸다. 키 자체가 없더라도 raw_comment.get 은 None 을 반환하고, 서비스
    # 계층이 안전하게 Minor 로 폴백한다.
    return (
        {
            "path": path,
            "line": line_number,
            "body": body,
            "severity": raw_comment.get("severity"),
        },
        None,
    )


def normalize_response(raw_response: dict[str, Any], *, max_findings: int) -> tuple[dict[str, Any], dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    dropped_comment_reasons: dict[str, int] = {}

    raw_comments = raw_response.get("comments", [])
    raw_comment_count = len(raw_comments) if isinstance(raw_comments, list) else 0

    for raw_comment in raw_comments if isinstance(raw_comments, list) else []:
        if not isinstance(raw_comment, dict):
            increment_reason(dropped_comment_reasons, "non_object_comment")
            continue
        normalized, reason = normalize_comment(raw_comment)
        if normalized is None:
            increment_reason(dropped_comment_reasons, reason or "invalid_comment")
            continue
        identity = (normalized["path"], normalized["line"], normalized["body"])
        if identity in seen:
            increment_reason(dropped_comment_reasons, "duplicate_comment")
            continue
        seen.add(identity)
        comments.append(normalized)
        if len(comments) >= max_findings:
            break

    summary = sanitize_korean_text(raw_response.get("summary"), DEFAULT_SUMMARY)
    positives = sanitize_items(normalize_text_list(raw_response.get("positives")), max_items=10)
    must_fix = sanitize_items(normalize_text_list(raw_response.get("must_fix")), max_items=10)
    suggestions = sanitize_items(normalize_text_list(raw_response.get("suggestions")), max_items=10)
    # 구 스키마(concerns 단일 필드)도 통과시키고, 서비스 계층에서 risk marker 기반으로
    # must_fix / suggestions 로 흡수한다. 파서는 분류 기준을 몰라도 되도록 원본만 넘긴다.
    # salvage 경로가 이미 legacy_concerns 키를 채웠을 수 있으니 먼저 그 키를 확인한다.
    raw_legacy = raw_response.get("legacy_concerns")
    if not raw_legacy:
        raw_legacy = raw_response.get("concerns")
    legacy_concerns = sanitize_items(normalize_text_list(raw_legacy), max_items=10)
    event = normalize_event_value(str(raw_response.get("event") or ""), has_comments=bool(comments))

    normalized_response = {
        "summary": summary,
        "event": event,
        "positives": positives or list(DEFAULT_POSITIVES),
        "must_fix": must_fix,
        "suggestions": suggestions,
        "legacy_concerns": legacy_concerns,
        "comments": comments,
    }
    metadata = {
        "raw_comment_count": raw_comment_count,
        "normalized_comment_count": len(comments),
        "dropped_comment_reasons": dropped_comment_reasons,
    }
    return normalized_response, metadata


def parse_and_normalize_model_output(raw_output: str, *, max_findings: int = DEFAULT_MAX_FINDINGS) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        parsed, parse_meta = parse_model_json(raw_output)
    except RuntimeError as exc:
        parsed = fallback_response()
        parse_meta = {
            "parse_mode": "fallback_response",
            "parse_error": normalize_text(str(exc)),
        }

    normalized_response, normalization_meta = normalize_response(parsed, max_findings=max_findings)
    metadata = {
        **parse_meta,
        **normalization_meta,
    }
    return normalized_response, metadata
