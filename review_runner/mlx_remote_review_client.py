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
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

from review_runner.mlx_review_parser import (
    DEFAULT_MAX_FINDINGS,
    parse_and_normalize_model_output,
)
from review_runner.mlx_review_prompt import build_messages


DEFAULT_GENERATE_URL = "http://127.0.0.1:8002/v1/generate"
DEFAULT_MAX_TOKENS = 1200
DEFAULT_TIMEOUT_SECONDS = 600.0
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


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
    """MLX_GENERATE_URL 을 반환하기 전에 스킴을 강제 검증한다. file:// 같은 위험한
    스킴이 들어오면 urllib 가 로컬 파일을 열어 스푸핑된 응답을 받을 위험이 있다."""
    url = os.environ.get("MLX_GENERATE_URL", DEFAULT_GENERATE_URL)
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise RuntimeError(
            f"MLX_GENERATE_URL must use http or https scheme, got: {parsed.scheme or '(empty)'}"
        )
    if not parsed.netloc:
        raise RuntimeError(f"MLX_GENERATE_URL must include a host, got: {url}")
    return url


def _auth_token() -> str:
    """``MLX_GENERATE_AUTH_TOKEN`` — 서버가 같은 값을 검증한다. 비어 있으면 헤더 미송신
    (서버 쪽도 인증 OFF 일 때만 동작). 운영에서는 양쪽이 동시에 설정돼야 한다."""
    return os.environ.get("MLX_GENERATE_AUTH_TOKEN", "").strip()


def _model_label() -> str:
    """리뷰 푸터에 노출할 모델 이름. 응답에서 받은 값을 우선 쓰고, 없으면 env 로 폴백."""
    return os.environ.get("MLX_MODEL", "")


def _build_request_body(messages: list[dict[str, str]]) -> dict[str, Any]:
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
    return request_body


def _build_request(url: str, body: bytes) -> urllib.request.Request:
    headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
    token = _auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=body, method="POST", headers=headers)


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    """HTTPError 본문을 best-effort 로 읽되 IO 실패는 좁게 잡고 stderr 에 남긴다.
    바깥쪽 RuntimeError 메시지에 본문을 같이 담아 운영자가 401/400 원인을 한 번에
    본다."""
    try:
        return exc.read().decode("utf-8", errors="replace")
    except (OSError, AttributeError) as read_exc:
        print(
            f"[mlx-remote-client] failed to read HTTP error body for {exc.code}: {read_exc}",
            file=sys.stderr,
        )
        return ""


def _post_generate(messages: list[dict[str, str]]) -> dict[str, Any]:
    """``/v1/generate`` 호출. 일시적 네트워크 오류는 1회 재시도해 webhook 단발 실패
    가능성을 낮춘다 (HTTPError 는 retry 안 함 — 4xx/5xx 는 재시도해도 같은 결과)."""
    url = _generate_url()
    request_body = _build_request_body(messages)
    encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    timeout = _get_env_float("MLX_GENERATE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)

    last_url_error: urllib.error.URLError | None = None
    for attempt in range(2):
        request = _build_request(url, encoded)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
            break
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            raise RuntimeError(
                f"MLX generate endpoint returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            last_url_error = exc
            if attempt == 0:
                # 짧은 backoff. mlx-final-py 가 final-reply 처리로 잠깐 응답을 못
                # 받는 경우가 흔하므로 1 초만 쉬고 한 번 더 친다.
                time.sleep(1.0)
                continue
            raise RuntimeError(
                f"Failed to reach MLX generate endpoint at {url} after retry: {exc.reason}"
            ) from exc
    else:
        # 이론상 unreachable — 위 break / raise 로 빠져나가야 함.
        if last_url_error is not None:
            raise RuntimeError(
                f"Failed to reach MLX generate endpoint at {url}: {last_url_error.reason}"
            )
        raise RuntimeError(f"Failed to reach MLX generate endpoint at {url}")

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
