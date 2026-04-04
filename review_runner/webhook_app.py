#!/usr/bin/env python3
"""FastAPI webhook server for GitHub PR reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from review_runner.review_service import DEFAULT_API_URL, resolve_github_token, review_pull_request


SUPPORTED_PULL_REQUEST_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

app = FastAPI(title="GitHub MLX Review Webhook", version="1.0.0")


def require_env(name: str) -> str:
    """필수 환경변수가 비어 있으면 서버 시작 대신 명확한 오류를 낸다."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> None:
    """GitHub webhook 서명을 검증해 위조 요청을 초기에 차단한다."""
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def should_process_pull_request(event: dict[str, Any]) -> tuple[bool, str]:
    """리뷰를 실제로 돌릴 pull_request 액션만 통과시킨다."""
    action = event.get("action")
    pull_request = event.get("pull_request") or {}

    if action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return False, f"Unsupported pull_request action: {action}"
    if pull_request.get("draft"):
        return False, "Draft pull requests are ignored"
    return True, ""


def build_delivery_prefix(delivery_id: str | None) -> str:
    """서버 로그에서 같은 webhook 흐름을 쉽게 묶어보기 위한 접두사다."""
    return f"[delivery={delivery_id}] " if delivery_id else ""


def describe_exception(exc: Exception) -> str:
    """비어 있지 않은 예외 메시지를 만들어 구조화 로그에 넣는다."""
    message = str(exc).strip()
    return message or exc.__class__.__name__


def build_failed_review_result(
    repository: str,
    pull_number: int,
    delivery_id: str | None,
    stage: str,
    error: Exception,
    *,
    auth_source: str | None = None,
) -> dict[str, Any]:
    """백그라운드 작업 실패를 운영 로그에서 바로 읽을 수 있는 구조로 정리한다."""
    return {
        "status": "failed",
        "repository": repository,
        "pull_number": pull_number,
        "delivery_id": delivery_id,
        "stage": stage,
        "error_type": error.__class__.__name__,
        "error": describe_exception(error),
        "auth_source": auth_source,
    }


def extract_pull_request_target(event: dict[str, Any]) -> tuple[str, int]:
    """payload에서 리뷰 대상 저장소와 PR 번호를 꺼낸다."""
    repository = (event.get("repository") or {}).get("full_name")
    pull_request = event.get("pull_request") or {}
    pull_number = pull_request.get("number")
    if not repository or not isinstance(pull_number, int):
        raise HTTPException(status_code=400, detail="Missing repository or pull_request.number")
    return repository, pull_number


def handle_pull_request_event(repository: str, pull_number: int, delivery_id: str | None) -> None:
    """백그라운드 스레드에서 실제 리뷰 생성과 GitHub 등록을 처리한다."""
    started_at = time.monotonic()
    prefix = build_delivery_prefix(delivery_id)
    print(f"{prefix}Starting review for {repository}#{pull_number}", flush=True)
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL)
    auth_source: str | None = None

    try:
        auth = resolve_github_token(repository=repository, api_url=api_url)
        auth_source = auth.source
        print(f"{prefix}Resolved GitHub auth via {auth_source}", flush=True)
    except Exception as exc:
        duration = time.monotonic() - started_at
        failure_result = build_failed_review_result(
            repository,
            pull_number,
            delivery_id,
            "auth_resolution",
            exc,
        )
        print(f"{prefix}Review failed in {duration:.1f}s during auth_resolution: {failure_result['error']}", flush=True)
        print(prefix + json.dumps(failure_result, ensure_ascii=False))
        return

    try:
        result = review_pull_request(
            repository=repository,
            pull_number=pull_number,
            token=auth.token,
            api_url=api_url,
            dry_run=os.environ.get("DRY_RUN") == "1",
            auth_source=auth_source,
            log_prefix=prefix,
        )
    except Exception as exc:
        duration = time.monotonic() - started_at
        failure_result = build_failed_review_result(
            repository,
            pull_number,
            delivery_id,
            "review_execution",
            exc,
            auth_source=auth_source,
        )
        print(
            f"{prefix}Review failed in {duration:.1f}s during review_execution: {failure_result['error']}",
            flush=True,
        )
        print(prefix + json.dumps(failure_result, ensure_ascii=False))
        return

    duration = time.monotonic() - started_at
    print(f"{prefix}Review finished in {duration:.1f}s", flush=True)
    print(prefix + json.dumps(result, ensure_ascii=False))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    """로드밸런서와 수동 점검용 최소 헬스체크 엔드포인트다."""
    return {"status": "ok"}


@app.post("/github/webhook", status_code=202)
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """GitHub webhook을 검증한 뒤 빠르게 202를 반환하고 리뷰는 백그라운드에서 처리한다."""
    body = await request.body()
    secret = require_env("GITHUB_WEBHOOK_SECRET")
    verify_signature(body, request.headers.get("X-Hub-Signature-256"), secret)

    try:
        event = json.loads(body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery")

    if event_type == "ping":
        return {"status": "ok", "event": "ping", "delivery_id": delivery_id}

    if event_type != "pull_request":
        return {"status": "ignored", "reason": f"Unsupported event: {event_type}", "delivery_id": delivery_id}

    should_process, reason = should_process_pull_request(event)
    if not should_process:
        return {"status": "ignored", "reason": reason, "delivery_id": delivery_id}

    repository, pull_number = extract_pull_request_target(event)
    # GitHub에는 빠르게 202를 돌려주고, 무거운 리뷰 작업은 별도 스레드에서 이어간다.
    background_tasks.add_task(handle_pull_request_event, repository, pull_number, delivery_id)
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "repository": repository,
        "pull_number": pull_number,
    }
