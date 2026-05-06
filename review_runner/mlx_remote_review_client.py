#!/usr/bin/env python3
"""HTTP-based PR review client.

mlx-final-py 의 ``POST /v1/generate`` 엔드포인트로 chat messages 를 보내고
raw 텍스트를 받아 기존 파서를 통과시킨다. mlx-lm import 자체를 안 하므로
이 모듈을 사용하는 webhook 프로세스에는 모델이 메모리에 올라가지 않고,
모델 인스턴스는 mlx-final-py 프로세스에 한 번만 상주한다.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from review_runner.mlx_review_parser import (
    DEFAULT_MAX_FINDINGS,
    parse_and_normalize_model_output,
)
from review_runner.mlx_review_prompt import build_messages


DEFAULT_GENERATE_URL = "http://127.0.0.1:8002/v1/generate"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_TIMEOUT_SECONDS = 600.0


def _get_env_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _get_env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float") from exc


def _get_optional_env_float(name: str) -> float | None:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        return float(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a float") from exc


def _get_optional_env_int(name: str) -> int | None:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return None
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _generate_url() -> str:
    return os.environ.get("MLX_GENERATE_URL", DEFAULT_GENERATE_URL)


def _model_label() -> str:
    """리뷰 푸터에 노출할 모델 이름. 응답에서 받은 값을 우선 쓰고, 없으면 env 로 폴백."""
    return os.environ.get("MLX_MODEL", "")


def _post_generate(messages: list[dict[str, str]]) -> dict[str, Any]:
    request_body: dict[str, Any] = {
        "messages": messages,
        "max_tokens": _get_env_int("MLX_MAX_TOKENS", DEFAULT_MAX_TOKENS),
        "temperature": _get_env_float("MLX_TEMPERATURE", 0.0),
        "top_p": _get_env_float("MLX_TOP_P", 1.0),
    }
    rep_penalty = _get_optional_env_float("MLX_REPETITION_PENALTY")
    if rep_penalty is not None:
        request_body["repetition_penalty"] = rep_penalty
    rep_ctx = _get_optional_env_int("MLX_REPETITION_CONTEXT_SIZE")
    if rep_ctx is not None:
        request_body["repetition_context_size"] = rep_ctx

    encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    timeout = _get_env_float("MLX_GENERATE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    request = urllib.request.Request(
        _generate_url(),
        data=encoded,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 응답 본문을 로그에 남겨야 운영자가 원인을 본다.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise RuntimeError(
            f"MLX generate endpoint returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Failed to reach MLX generate endpoint at {_generate_url()}: {exc.reason}"
        ) from exc

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"MLX generate endpoint returned non-JSON body:\n{response_body}"
        ) from exc

    if not isinstance(parsed, dict) or not parsed.get("ok") or not isinstance(parsed.get("text"), str):
        raise RuntimeError(f"MLX generate endpoint returned unexpected payload:\n{response_body}")
    return parsed


def review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """payload 를 원격 generate endpoint 로 보내고, 결과를 계약에 맞는 JSON 으로 정리한다."""
    max_findings = _get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS)
    messages = build_messages(payload, max_findings=max_findings)
    response = _post_generate(messages)
    raw_output = response["text"]
    normalized_response, metadata = parse_and_normalize_model_output(
        raw_output,
        max_findings=max_findings,
    )
    metadata["model_name"] = response.get("model") or _model_label()
    metadata["backend"] = "remote"
    metadata["generate_url"] = _generate_url()
    if "elapsed_ms" in response:
        metadata["remote_elapsed_ms"] = response["elapsed_ms"]
    normalized_response["_meta"] = metadata
    return normalized_response
