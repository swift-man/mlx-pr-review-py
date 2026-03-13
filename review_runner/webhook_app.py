#!/usr/bin/env python3
"""FastAPI webhook server for GitHub PR reviews."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from review_runner.review_service import DEFAULT_API_URL, review_pull_request


SUPPORTED_PULL_REQUEST_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

app = FastAPI(title="GitHub MLX Review Webhook", version="1.0.0")


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def verify_signature(payload: bytes, signature_header: str | None, secret: str) -> None:
    if not signature_header:
        return

    expected = "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")


def should_process_pull_request(event: dict[str, Any]) -> tuple[bool, str]:
    action = event.get("action")
    pull_request = event.get("pull_request") or {}

    if action not in SUPPORTED_PULL_REQUEST_ACTIONS:
        return False, f"Unsupported pull_request action: {action}"
    if pull_request.get("draft"):
        return False, "Draft pull requests are ignored"
    return True, ""


def handle_pull_request_event(repository: str, pull_number: int, delivery_id: str | None) -> None:
    token = require_env("GITHUB_TOKEN")
    print(f"Using GitHub token: {token}")
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL)
    result = review_pull_request(
        repository=repository,
        pull_number=pull_number,
        token=token,
        api_url=api_url,
        dry_run=os.environ.get("DRY_RUN") == "1",
    )
    prefix = f"[delivery={delivery_id}] " if delivery_id else ""
    print(prefix + json.dumps(result, ensure_ascii=False))


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/github/webhook", status_code=202)
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
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

    repository = (event.get("repository") or {}).get("full_name")
    pull_request = event.get("pull_request") or {}
    pull_number = pull_request.get("number")
    if not repository or not isinstance(pull_number, int):
        raise HTTPException(status_code=400, detail="Missing repository or pull_request.number")

    background_tasks.add_task(handle_pull_request_event, repository, pull_number, delivery_id)
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "repository": repository,
        "pull_number": pull_number,
    }
