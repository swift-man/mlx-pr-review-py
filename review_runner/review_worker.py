#!/usr/bin/env python3
"""Redis stream 에서 리뷰 job 을 꺼내 MLX 리뷰를 수행하는 worker.

receiver (pr-review-receiver) 가 webhook 을 검증해 큐에 넣고, 이 프로세스가 꺼내
리뷰를 만들어 GitHub Review API 로 등록한다. receiver 와 달리 10초 예산이 없으므로
여기서는 모델을 마음껏 돌려도 된다.

여러 대를 띄워도 된다. consumer group 이 job 을 한 번만 배분하고, 죽은 워커가 물고
있던 job 은 XAUTOCLAIM 이 회수한다.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import time
from typing import Any

import redis

from review_runner import review_queue
from review_runner.review_service import DEFAULT_API_URL, resolve_github_token, review_pull_request


# Redis 가 잠깐 끊겼을 때 즉시 죽지 않고 재연결을 기다린다. webhook 은 receiver 가
# 계속 받아 큐에 쌓고 있으므로, worker 는 복구되는 대로 이어서 처리하면 된다.
REDIS_RETRY_DELAY_SECONDS = 5.0

_SHUTDOWN = False


def _request_shutdown(signum: int, _frame: Any) -> None:
    """진행 중인 리뷰는 끝까지 마치고 루프를 빠져나간다.

    리뷰 도중 강제 종료하면 그 job 은 ACK 되지 않은 채 PEL 에 남고, 나중에
    XAUTOCLAIM 이 회수해 재처리한다. 유실은 없지만 같은 리뷰를 두 번 돌리게 되므로
    가능하면 정상 종료를 기다리는 편이 낫다.
    """
    global _SHUTDOWN
    _SHUTDOWN = True
    log("shutdown_requested", signal=signum)


def log(message: str, **fields: Any) -> None:
    print(json.dumps({"msg": message, **fields}, ensure_ascii=False), flush=True)


def consumer_name() -> str:
    """머신마다 달라야 한다 — 2대 구성에서 같은 이름을 쓰면 PEL 이 뒤섞인다."""
    override = os.environ.get("REVIEW_WORKER_NAME")
    if override:
        return override
    return f"{socket.gethostname()}-{os.getpid()}"


def max_attempts() -> int:
    try:
        return max(1, int(os.environ.get("REVIEW_WORKER_MAX_ATTEMPTS", "")))
    except ValueError:
        return review_queue.DEFAULT_MAX_ATTEMPTS


def reclaim_idle_ms() -> int:
    try:
        return max(1000, int(os.environ.get("REVIEW_WORKER_RECLAIM_IDLE_MS", "")))
    except ValueError:
        return review_queue.DEFAULT_RECLAIM_IDLE_MS


def run_review_job(client: redis.Redis, job: dict[str, Any], prefix: str) -> dict[str, Any]:
    """실제 리뷰 실행. should_continue 로 stale 판정을 리뷰 도중에도 계속 확인한다."""
    api_url = os.environ.get("GITHUB_API_URL", DEFAULT_API_URL)
    auth = resolve_github_token(repository=job["repository"], api_url=api_url)

    def still_current() -> bool:
        # 리뷰가 수 분 걸리는 동안 새 push 가 들어오면 오래된 코드 기준 리뷰를
        # 게시하지 않도록 중단시킨다. Redis 가 잠깐 안 되면 중단하지 않고 계속한다
        # (리뷰를 빠뜨리는 쪽이 더 나쁘다).
        try:
            return not review_queue.is_stale(client, job)
        except redis.RedisError:
            return True

    return review_pull_request(
        repository=job["repository"],
        pull_number=job["pull_number"],
        token=auth.token,
        api_url=api_url,
        dry_run=os.environ.get("DRY_RUN") == "1",
        auth_source=auth.source,
        should_continue=still_current,
        log_prefix=prefix,
    )


def process_message(client: redis.Redis, message_id: str, fields: dict[str, str]) -> None:
    try:
        job = review_queue.parse_job(fields)
    except ValueError as exc:
        # 스키마가 깨진 job 은 몇 번을 다시 시도해도 같은 결과다. 바로 격리한다.
        log("job_malformed", message_id=message_id, error=str(exc))
        review_queue.send_to_dead_letter(client, message_id, fields, f"malformed: {exc}")
        return

    prefix = f"[delivery={job['delivery_id']}] "
    attempts = review_queue.record_attempt(client, message_id)
    limit = max_attempts()

    if attempts > limit:
        log(
            "job_exhausted",
            message_id=message_id,
            attempts=attempts,
            repository=job["repository"],
            pull_number=job["pull_number"],
        )
        review_queue.send_to_dead_letter(client, message_id, fields, f"max attempts ({limit}) exceeded")
        return

    if review_queue.is_stale(client, job):
        # 이 job 이 큐에 있는 동안 같은 PR 에 새 push 가 들어왔다. 오래된 코드를
        # 리뷰해봐야 버려지므로 모델을 돌리기 전에 버린다.
        log(
            "job_stale_skipped",
            message_id=message_id,
            repository=job["repository"],
            pull_number=job["pull_number"],
            job_head=job["head_sha"][:12],
        )
        review_queue.ack(client, message_id)
        return

    started_at = time.monotonic()
    log(
        "job_started",
        message_id=message_id,
        attempt=attempts,
        repository=job["repository"],
        pull_number=job["pull_number"],
        head=job["head_sha"][:12],
    )

    try:
        result = run_review_job(client, job, prefix)
    except Exception as exc:  # noqa: BLE001 - 어떤 실패든 job 을 잃지 않는 게 우선
        # ACK 하지 않고 빠져나간다. PEL 에 남아 min-idle-time 뒤 XAUTOCLAIM 이
        # 회수하고, attempts 가 한도를 넘으면 dead letter 로 간다.
        log(
            "job_failed",
            message_id=message_id,
            attempt=attempts,
            error_type=exc.__class__.__name__,
            error=str(exc) or exc.__class__.__name__,
            elapsed=round(time.monotonic() - started_at, 1),
        )
        return

    review_queue.ack(client, message_id)
    log(
        "job_finished",
        message_id=message_id,
        elapsed=round(time.monotonic() - started_at, 1),
        status=result.get("status"),
        repository=job["repository"],
        pull_number=job["pull_number"],
    )


def run_forever() -> None:
    consumer = consumer_name()
    client = review_queue.build_redis_client()
    log("worker_starting", consumer=consumer, stream=review_queue.STREAM_KEY, redis=review_queue.redis_url())

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    while not _SHUTDOWN:
        try:
            review_queue.ensure_consumer_group(client)

            # 먼저 버려진 job 부터 회수한다. 새 job 만 계속 집어가면 죽은 워커가
            # 남긴 작업이 영원히 처리되지 않는다.
            for message_id, fields in review_queue.claim_abandoned_jobs(
                client, consumer, min_idle_ms=reclaim_idle_ms()
            ):
                log("job_reclaimed", message_id=message_id)
                process_message(client, message_id, fields)
                if _SHUTDOWN:
                    break
            if _SHUTDOWN:
                break

            for message_id, fields in review_queue.read_new_jobs(client, consumer):
                process_message(client, message_id, fields)
                if _SHUTDOWN:
                    break
        except redis.RedisError as exc:
            log("redis_error", error_type=exc.__class__.__name__, error=str(exc))
            time.sleep(REDIS_RETRY_DELAY_SECONDS)

    log("worker_stopped", consumer=consumer)


def main() -> int:
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
