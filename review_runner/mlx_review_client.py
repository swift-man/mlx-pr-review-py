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


DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_MAX_FINDINGS = 10
DEFAULT_SUMMARY = "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."
MAX_PARSE_ERROR_SNIPPET = 2000

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

_MODEL = None
_TOKENIZER = None
_LOAD_LOCK = threading.Lock()


def get_env_bool(name: str, default: bool = False) -> bool:
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
    max_findings = get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS)
    compact_payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [
        {
            "role": "system",
            "content": (
                "You are a senior software engineer performing pull request review. "
                "Return exactly one JSON object and nothing else. "
                "Never wrap the answer in markdown fences. "
                "Report only high-confidence issues that are directly visible in the diff. "
                "Use strict JSON syntax with double-quoted keys and string values. "
                "Do not use trailing commas, single quotes, comments, or unquoted enum values. "
                "All output must be written in Korean. This is mandatory. "
                "Write summary, positives, concerns, and every line comment body in Korean only. "
                "Use only these top-level keys: summary, event, positives, concerns, comments. "
                "positives and concerns must be JSON arrays, never inline labels such as positive1: or concerns1:. "
                "Do not put positives, concerns, or comments inside the summary string. "
                "Do not use English sentences in JSON values unless a file path, symbol, or API name requires it. "
                f"Return at most {max_findings} findings. "
                "Do not write praise-only line comments. "
                'If there are no actionable issues, return {"summary":"...","event":"COMMENT","positives":["..."],"concerns":[],"comments":[]} '
                "and use summary plus positives to briefly mention what looks strong about the diff in Korean."
            ),
        },
        {
            "role": "user",
            "content": (
                "Review this pull request diff payload and respond using the response_schema inside it. "
                "모든 출력은 반드시 한국어로 작성하세요.\n"
                f"{compact_payload}"
            ),
        },
    ]


def render_prompt(tokenizer: Any, messages: list[dict[str, str]]) -> str:
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


def format_error_snippet(text: str, limit: int = MAX_PARSE_ERROR_SNIPPET) -> str:
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


def salvage_broken_output(candidate: str) -> dict[str, Any] | None:
    summary = extract_string_field(candidate, "summary", SUMMARY_STOP_RE)
    event = extract_string_field(candidate, "event", GENERIC_FIELD_STOP_RE).upper()
    positives = normalize_text_list(extract_array_field(candidate, "positives"), max_items=10)
    concerns = normalize_text_list(extract_array_field(candidate, "concerns"), max_items=10)
    comments_raw = extract_array_field(candidate, "comments") or []
    comments = [item for item in comments_raw if isinstance(item, dict)]

    if not positives:
        positives = extract_labeled_items(candidate, POSITIVE_ITEM_RE)
    if not concerns:
        concerns = extract_labeled_items(candidate, CONCERN_ITEM_RE)

    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        event = "REQUEST_CHANGES" if comments else "COMMENT"

    if not any([summary, positives, concerns, comments]):
        return None

    return {
        "summary": summary or DEFAULT_SUMMARY,
        "event": event,
        "positives": positives,
        "concerns": concerns,
        "comments": comments,
    }


def parse_model_json(raw_output: str) -> dict[str, Any]:
    candidate = extract_json_object(raw_output)
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

    event = str(raw_response.get("event") or "").strip().upper()
    if event not in {"COMMENT", "REQUEST_CHANGES"}:
        event = "REQUEST_CHANGES" if comments else "COMMENT"
    if not comments:
        event = "COMMENT"

    return {
        "summary": summary,
        "event": event,
        "positives": positives,
        "concerns": concerns,
        "comments": comments,
    }


def review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    model, tokenizer = load_runtime()
    del model
    messages = build_messages(payload)
    prompt = render_prompt(tokenizer, messages)
    raw_output = run_generation(prompt)
    try:
        parsed = parse_model_json(raw_output)
    except RuntimeError as exc:
        raise RuntimeError(
            "Failed to parse MLX output as JSON.\n"
            f"Raw output:\n{format_error_snippet(raw_output)}"
        ) from exc
    return normalize_response(parsed)


def main(argv: list[str] | None = None) -> int:
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
