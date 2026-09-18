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
    """머신마다 달라야 하고, 재기동 사이에는 **같아야** 한다.

    pid 를 넣으면 재기동할 때마다 consumer group 에 새 이름이 쌓인다. 죽은 consumer
    는 스스로 사라지지 않아서 XINFO GROUPS 의 consumers 가 계속 늘어난다 (운영 중
    재기동 3회만에 3개가 됐다). 이름을 고정하면 재기동 후 자기 PEL 을 그대로
    이어받을 수 있다는 이점도 있다.

    한 머신에서 워커를 여러 개 돌려야 하면 REVIEW_WORKER_NAME 으로 구분한다.
    """
    override = os.environ.get("REVIEW_WORKER_NAME")
    if override:
        return override
    return socket.gethostname()


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

    # stale 검사를 재시도 카운트보다 먼저 한다. 순서가 반대면 큐에서 대기하는 동안
    # 무효가 된 job 도 INCR 을 소모하고, 한도에 걸린 상태로 stale 이 되면 '건너뜀'
    # 이 아니라 dead letter 로 잘못 격리돼 거짓 경보가 된다.
    #
    # Redis 가 잠깐 안 되면 stale 판정을 포기하고 그냥 처리한다. 리뷰를 빠뜨리는
    # 쪽이 중복 리뷰보다 나쁘고, 여기서 예외를 올리면 방금 집어든 job 이 ACK 되지
    # 않은 채 최소 min_idle_ms 동안 방치된다.
    try:
        stale = review_queue.is_stale(client, job)
    except redis.RedisError as exc:
        log("stale_check_failed", message_id=message_id, error=str(exc))
        stale = False
    if stale:
        log(
            "job_stale_skipped",
            message_id=message_id,
            repository=job["repository"],
            pull_number=job["pull_number"],
            job_head=job["head_sha"][:12],
        )
        review_queue.ack(client, message_id)
        return

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
        detail = str(exc) or exc.__class__.__name__
        log(
            "job_failed",
            message_id=message_id,
            attempt=attempts,
            error_type=exc.__class__.__name__,
            error=detail,
            elapsed=round(time.monotonic() - started_at, 1),
        )
        if attempts >= limit:
            # 마지막 시도였다. 여기서 ACK 없이 돌아가면 min_idle_ms 를 기다렸다가
            # 다음 회수 주기에 'max attempts exceeded' 라는 일반 문구로만 격리되어
            # 실제 실패 원인이 사라진다. 지금 원인과 함께 격리한다.
            review_queue.send_to_dead_letter(
                client,
                message_id,
                fields,
                f"attempt {attempts}/{limit} failed: {exc.__class__.__name__}: {detail}",
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
    log(
        "worker_starting",
        consumer=consumer,
        stream=review_queue.STREAM_KEY,
        # 비밀번호를 제거한 값만 남긴다. LaunchAgent 로그는 /tmp 에
        # world-readable 로 생성된다.
        redis=review_queue.sanitized_redis_url(),
    )

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    # 기동 시점에 Redis 가 잠깐 안 될 수 있다 (링크 재협상, Redis 재기동 등).
    # 여기서 예외가 그대로 올라가면 프로세스가 죽고, KeepAlive 가 곧바로 되살려
    # 같은 지점에서 또 죽는 crashloop 가 된다. 실제로 Thunderbolt 링크가 순간
    # 끊겼을 때 이 경로에서 'No route to host' 로 죽었다.
    #
    # 루프 안의 RedisError 처리와 같은 방식으로, 연결될 때까지 기다린다.
    while not _SHUTDOWN:
        try:
            # 루프 안에서 매번 호출하면 정상 상태에서도 5초마다 XGROUP CREATE 를
            # 보내 BUSYGROUP 예외를 유발한다. 기동 시 한 번이면 충분하고, 그룹이
            # 사라지는 예외 상황은 아래 RedisError 경로에서 재생성된다.
            review_queue.ensure_consumer_group(client)

            # 재기동 직후 자기가 물고 있던 job 부터 이어받는다. consumer 이름이
            # 고정이라 이전 프로세스의 PEL 을 그대로 승계할 수 있다. 이게 없으면
            # min_idle_ms 를 기다려야만 회수된다.
            for message_id, fields in review_queue.read_own_pending(client, consumer):
                if _SHUTDOWN:
                    break
                log("job_resumed", message_id=message_id)
                process_message(client, message_id, fields)
            break
        except redis.RedisError as exc:
            log("redis_unavailable_at_startup", error_type=exc.__class__.__name__, error=str(exc))
            time.sleep(REDIS_RETRY_DELAY_SECONDS)

    while not _SHUTDOWN:
        try:
            # 먼저 버려진 job 부터 회수한다. 새 job 만 계속 집어가면 죽은 워커가
            # 남긴 작업이 영원히 처리되지 않는다.
            for message_id, fields in review_queue.claim_abandoned_jobs(
                client, consumer, min_idle_ms=reclaim_idle_ms()
            ):
                if _SHUTDOWN:
                    break
                log("job_reclaimed", message_id=message_id)
                process_message(client, message_id, fields)
            if _SHUTDOWN:
                break

            for message_id, fields in review_queue.read_new_jobs(client, consumer):
                # 종료 요청을 먼저 확인한다. 순서가 반대면 SIGTERM 을 받은 뒤에도
                # 수 분 걸리는 새 리뷰를 시작해버려, launchd 의 종료 유예(기본 20초)를
                # 넘겨 SIGKILL 당한다. 여기서 빠져나가면 job 은 ACK 되지 않은 채
                # 자기 PEL 에 남고, 재기동 시 read_own_pending 이 그대로 이어받는다.
                if _SHUTDOWN:
                    break
                process_message(client, message_id, fields)
        except redis.RedisError as exc:
            log("redis_error", error_type=exc.__class__.__name__, error=str(exc))
            time.sleep(REDIS_RETRY_DELAY_SECONDS)
            # 그룹 자체가 사라진 경우(FLUSHDB 등)를 대비해 재생성만 시도한다.
            try:
                review_queue.ensure_consumer_group(client)
            except redis.RedisError:
                pass

    log("worker_stopped", consumer=consumer)


def main() -> int:
    run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
