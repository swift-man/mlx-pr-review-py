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
from urllib.parse import urlparse, urlunparse

from review_runner.mlx_review_parser import (
    DEFAULT_MAX_FINDINGS,
    parse_and_normalize_model_output,
)
from review_runner.mlx_review_prompt import build_messages


DEFAULT_GENERATE_URL = "http://127.0.0.1:8002/v1/generate"
DEFAULT_MAX_TOKENS = 900
DEFAULT_TIMEOUT_SECONDS = 240.0
DEFAULT_CLIENT_MAX_BODY_BYTES = 1 * 1024 * 1024
ALLOWED_URL_SCHEMES = frozenset({"http", "https"})


def _get_env_int(name: str, default: int) -> int:
    """env 변수를 int 로 정규화. 미설정 또는 빈 문자열이면 default — ``export FOO=``
    처럼 값이 비워진 케이스도 default 로 떨어지도록 한다."""
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
        return default
    try:
        return int(raw_value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _get_env_float(name: str, default: float) -> float:
    raw_value = os.environ.get(name)
    if raw_value is None or raw_value.strip() == "":
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
    """MLX_GENERATE_URL 을 반환하기 전에 스킴/호스트/포트를 강제 검증한다.

    - ``file://`` / ``ftp://`` / ``data:`` 같은 위험 스킴은 RuntimeError — urllib 가
      로컬 파일을 열어 위장된 응답을 받는 SSRF 류 사고 차단.
    - ``http://:8002/path`` 처럼 hostname 이 비어 있는 형태도 거부 — netloc 만 있고
      hostname 이 비면 ``hmac`` / TLS 검증 등이 의도와 다르게 동작할 수 있다 (CodeRabbit
      Round 2 nitpick).
    - ``http://gpu:bad/`` 같은 잘못된 포트는 ``parsed.port`` 접근 시점에 ValueError 가
      나서 호출자가 추적하기 어려운 형태로 누수된다. 명시적으로 1-65535 범위 밖이거나
      파싱 실패면 RuntimeError 로 통일 (CodeRabbit Round 3 Minor).
    """
    raw_url = os.environ.get("MLX_GENERATE_URL", "")
    url = raw_url.strip() or DEFAULT_GENERATE_URL
    parsed = urlparse(url)
    safe_url = _sanitize_url_for_logging(url) or "(invalid URL)"
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        raise RuntimeError(
            f"MLX_GENERATE_URL must use http or https scheme, got: {parsed.scheme or '(empty)'}"
        )
    if not parsed.hostname:
        raise RuntimeError(f"MLX_GENERATE_URL must include a host, got: {safe_url}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"MLX_GENERATE_URL contains invalid port: {safe_url}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise RuntimeError(
            f"MLX_GENERATE_URL port must be in 1..65535, got: {port}"
        )
    return url


def _sanitize_url_for_logging(url: str) -> str:
    """리뷰 metadata 와 로그에 노출할 URL 에서 userinfo / query / fragment 를 제거.

    원문 URL 을 그대로 노출하면 ``http://user:secret@gpu/`` 같은 비밀이나 내부
    엔드포인트의 디버그 쿼리가 리뷰 결과로 흘러나갈 수 있다 (CodeRabbit Round 2
    Major). scheme + hostname (+port) + path 만 재구성해서 안전한 표기를 돌려준다.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        netloc = f"{host}:{port}"
    else:
        netloc = host
    if not netloc:
        return ""
    return urlunparse((parsed.scheme, netloc, parsed.path, "", "", ""))


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
    가능성을 낮춘다.

    재시도 정책:
    - TimeoutError: 즉시 실패. 서버가 요청을 받았지만 생성이 timeout 을 넘긴 경우라
      같은 긴 요청을 바로 다시 보내면 webhook 시간이 두 배로 늘어난다.
    - URLError (connection refused / DNS 등): 1회 retry. 가장 흔한 시나리오는
      mlx-final-py 가 final-reply 처리 중 응답 못 받는 경우.
    - HTTP 5xx (502 Bad Gateway / 503 Service Unavailable / 504): 1회 retry — 원격 모델
      서버 배포/과부하로 인한 일시 장애 가능성 (gemini Round 3 Major).
    - HTTP 4xx: 재시도해도 같은 결과라 즉시 raise — webhook 처리시간 낭비 방지.
    """
    url = _generate_url()
    request_body = _build_request_body(messages)
    encoded = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    max_body_bytes = _get_env_int("MLX_GENERATE_CLIENT_MAX_BODY_BYTES", DEFAULT_CLIENT_MAX_BODY_BYTES)
    if max_body_bytes > 0 and len(encoded) > max_body_bytes:
        raise RuntimeError(
            "MLX generate request body is too large "
            f"({len(encoded)} > {max_body_bytes} bytes). "
            "Reduce reviewed files with .reviewbot.yml or raise both "
            "MLX_GENERATE_CLIENT_MAX_BODY_BYTES and the generate server's MLX_HTTP_BODY_MAX_BYTES."
        )
    timeout = _get_env_float("MLX_GENERATE_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
    sanitized = _sanitize_url_for_logging(url)

    response_body: str | None = None
    for attempt in range(2):
        request = _build_request(url, encoded)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_body = response.read().decode("utf-8")
            break
        except TimeoutError as exc:
            raise RuntimeError(
                "MLX generate endpoint timed out "
                f"after {timeout:.1f}s while waiting for {sanitized}"
            ) from exc
        except urllib.error.HTTPError as exc:
            detail = _read_error_body(exc)
            if exc.code >= 500 and attempt == 0:
                # 일시적 5xx 는 재시도 가능. 4xx 는 즉시 raise.
                time.sleep(1.0)
                continue
            raise RuntimeError(
                f"MLX generate endpoint returned HTTP {exc.code}: {detail or exc.reason}"
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise RuntimeError(
                    "MLX generate endpoint timed out "
                    f"after {timeout:.1f}s while waiting for {sanitized}"
                ) from exc
            if attempt == 0:
                # 짧은 backoff. mlx-final-py 가 final-reply 처리로 잠깐 응답을 못
                # 받는 경우가 흔하므로 1 초만 쉬고 한 번 더 친다.
                time.sleep(1.0)
                continue
            raise RuntimeError(
                f"Failed to reach MLX generate endpoint at {sanitized} after retry: {exc.reason}"
            ) from exc
        except OSError as exc:
            if attempt == 0:
                # 짧은 backoff. mlx-final-py 가 final-reply 처리로 잠깐 응답을 못
                # 받는 경우가 흔하므로 1 초만 쉬고 한 번 더 친다.
                time.sleep(1.0)
                continue
            raise RuntimeError(
                f"Failed to reach MLX generate endpoint at {sanitized} after retry: {exc}"
            ) from exc

    if response_body is None:
        # 위 for 루프는 break 또는 raise 로 빠지므로 이 분기에 도달하지 않지만,
        # 정적 분석 측면에서 명시적 가드를 둔다.
        raise RuntimeError(f"Failed to reach MLX generate endpoint at {sanitized}")

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
    # generate_url 은 sanitize 후 저장 — 리뷰 결과 / 로그에 userinfo, query, secret
    # 이 흘러나가지 않도록 한다 (CodeRabbit Round 2 Major).
    metadata["generate_url"] = _sanitize_url_for_logging(_generate_url())
    if "elapsed_ms" in response:
        metadata["remote_elapsed_ms"] = response["elapsed_ms"]
    normalized_response["_meta"] = metadata
    return normalized_response
