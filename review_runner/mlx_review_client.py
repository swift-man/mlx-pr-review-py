#!/usr/bin/env python3
"""Run PR review generation with MLX + mlx-lm and return strict JSON."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from typing import Any

from review_runner.mlx_review_parser import (
    DEFAULT_MAX_FINDINGS,
    parse_and_normalize_model_output,
)
from review_runner.mlx_review_prompt import build_messages


DEFAULT_MODEL = "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit"
DEFAULT_MAX_TOKENS = 2400

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


def get_requested_device() -> str | None:
    """선택적 장치 override를 읽어 Metal 장애 시 CPU fallback을 허용한다."""
    raw_value = os.environ.get("MLX_DEVICE")
    if raw_value is None:
        return None

    device_name = raw_value.strip().lower()
    if device_name in {"", "auto", "default"}:
        return None
    if device_name not in {"cpu", "gpu"}:
        raise RuntimeError("MLX_DEVICE must be one of: auto, cpu, gpu")
    return device_name


def configure_default_device() -> str:
    """요청된 기본 장치를 적용하고, 값이 없으면 기존 MLX 기본 동작을 유지한다."""
    device_name = get_requested_device()
    if device_name is None:
        return "auto"

    try:
        import mlx.core as mx
    except ImportError as exc:
        raise RuntimeError(
            "MLX runtime is not installed. Install the review venv with `pip install -r review_runner/requirements.txt`."
        ) from exc

    target_device = getattr(mx, device_name)
    mx.set_default_device(target_device)
    return device_name


def load_runtime() -> tuple[Any, Any]:
    """모델과 토크나이저를 한 번만 로드해 웹훅 요청 사이에서 재사용한다."""
    configure_default_device()

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
            # webhook 프로세스 안에서 remote code 실행을 기본 허용하면 운영 리스크가 커진다.
            "trust_remote_code": get_env_bool("MLX_TRUST_REMOTE_CODE", default=False),
        }
        _MODEL, _TOKENIZER = load(get_model_name(), tokenizer_config=tokenizer_config)
        return _MODEL, _TOKENIZER


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
        # 구버전 mlx-lm 은 generate() 인자가 더 적을 수 있다.
        return generate(
            model,
            tokenizer,
            prompt=prompt,
            max_tokens=generation_kwargs["max_tokens"],
            verbose=False,
        )


def review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """payload를 모델에 전달하고, 결과를 계약에 맞는 JSON으로 정리한다."""
    model, tokenizer = load_runtime()
    del model
    messages = build_messages(payload, max_findings=get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS))
    prompt = render_prompt(tokenizer, messages)
    raw_output = run_generation(prompt)
    normalized_response, metadata = parse_and_normalize_model_output(
        raw_output,
        max_findings=get_env_int("MLX_MAX_FINDINGS", DEFAULT_MAX_FINDINGS),
    )
    # 내부 파이프라인에서만 쓰는 진단 정보라 GitHub payload 생성 단계에서 무시된다.
    normalized_response["_meta"] = metadata
    return normalized_response


def main(argv: list[str] | None = None) -> int:
    """CLI 진입점으로 warmup과 실제 리뷰 실행을 모두 담당한다."""
    parser = argparse.ArgumentParser(description="Run MLX-based PR review generation")
    parser.add_argument("--warmup", action="store_true", help="Load the configured MLX model and exit")
    args = parser.parse_args(argv)

    if args.warmup:
        load_runtime()
        print(
            json.dumps(
                {
                    "status": "ready",
                    "model": get_model_name(),
                    "device": get_requested_device() or "auto",
                },
                ensure_ascii=False,
            )
        )
        return 0

    payload = json.load(sys.stdin)
    result = review_payload(payload)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
