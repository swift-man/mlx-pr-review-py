#!/usr/bin/env python3
"""Run PR review generation with MLX + mlx-lm and return strict JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any


DEFAULT_MODEL = "mlx-community/Llama-3.2-3B-Instruct-4bit"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_MAX_FINDINGS = 10
DEFAULT_SUMMARY = "즉시 수정이 필요한 문제는 보이지 않습니다. 변경 범위가 명확하고 전체 흐름도 비교적 잘 드러납니다."

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
                "All output must be written in Korean. This is mandatory. "
                "Write summary, positives, concerns, and every line comment body in Korean only. "
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
    in_string = False
    escape = False
    for index in range(start, len(candidate)):
        char = candidate[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return candidate[start : index + 1]

    raise RuntimeError(f"Could not extract a complete JSON object from model output:\n{candidate}")


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
    parsed = json.loads(extract_json_object(raw_output))
    if not isinstance(parsed, dict):
        raise RuntimeError(f"Model returned a non-object JSON value: {parsed!r}")
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
