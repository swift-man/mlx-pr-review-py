"""Redis 큐 계약 (worker 측).

이 파일의 key/stream/field 이름은 receiver repo (pr-review-receiver) 와 **공유하는
계약**이다. 한쪽만 바꾸면 job 이 조용히 유실된다. 변경 시 JOB_SCHEMA_VERSION 을 올리고
양쪽 repo 를 같이 배포할 것. tests/test_review_queue.py 가 리터럴 값을 고정해 두었으므로
이름을 잘못 바꾸면 테스트가 먼저 깨진다.

  prr:review_jobs                  stream. 리뷰 job 본문.
  prr:workers                      consumer group.
  prr:delivery:<delivery_id>       X-GitHub-Delivery 중복 제거 마커 (receiver 가 관리).
  prr:latest_head:<repo>#<pr>      해당 PR 의 최신 head sha. stale 판정 기준.
  prr:attempts:<message_id>        job 재시도 횟수. poison job 무한 재처리 방지.
  prr:review_dead                  최대 재시도를 넘긴 job 의 dead letter stream.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

import redis


JOB_SCHEMA_VERSION = "1"

KEY_PREFIX = "prr:"
STREAM_KEY = f"{KEY_PREFIX}review_jobs"
CONSUMER_GROUP = f"{KEY_PREFIX}workers"
DEAD_LETTER_KEY = f"{KEY_PREFIX}review_dead"

# 리뷰 1건은 수 분이 걸린다. 정상 처리 중인 job 을 다른 워커가 뺏어가면 같은 PR 을
# 두 번 리뷰하게 되므로, 회수 기준 시간은 리뷰 최대 소요시간보다 넉넉히 길어야 한다.
# MLX_GENERATE_TIMEOUT 기본값이 900초라서 그 두 배 이상을 기본으로 둔다.
DEFAULT_RECLAIM_IDLE_MS = 30 * 60 * 1000

# 같은 job 을 몇 번까지 다시 시도할지. 이 횟수를 넘으면 dead letter 로 보내고 ACK 한다.
# 없으면 항상 터지는 job 하나가 XAUTOCLAIM 루프를 영원히 점유한다.
DEFAULT_MAX_ATTEMPTS = 3

ATTEMPTS_TTL_SECONDS = 24 * 60 * 60

# XAUTOCLAIM cursor 를 따라가는 최대 반복 횟수. PEL 이 비정상적으로 크더라도
# 회수 루프가 한 주기를 독점하지 않게 상한을 둔다.
MAX_RECLAIM_SCANS = 20

# dead letter 보존 상한. 운영자가 원인 분석할 만큼만 남기고 그 이상은 버린다.
DEAD_LETTER_MAXLEN = 1000


def redis_url() -> str:
    return os.environ.get("REVIEW_REDIS_URL", "redis://127.0.0.1:6379/0")


def build_redis_client(url: str | None = None) -> redis.Redis:
    """worker 는 XREADGROUP 을 block 으로 대기하므로 socket timeout 을 길게 잡는다.

    receiver 와 달리 10초 예산이 없다. block 시간보다 짧게 잡으면 정상 대기가
    timeout 으로 오인된다.
    """
    return redis.Redis.from_url(
        url or redis_url(),
        decode_responses=True,
        socket_timeout=60.0,
        socket_connect_timeout=5.0,
        health_check_interval=30,
    )


def sanitized_redis_url(url: str | None = None) -> str:
    """로그에 노출할 Redis URL 에서 비밀번호를 제거한다.

    REVIEW_REDIS_URL 은 ``redis://:<password>@host:port/db`` 형태라 그대로 찍으면
    운영 로그에 비밀번호가 평문으로 남는다. LaunchAgent 로그는 /tmp 에 world-readable
    로 생성되므로 특히 위험하다.
    """
    raw = url or redis_url()
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.password:
        return raw
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = parsed.username or ""
    return urllib.parse.urlunsplit(
        (parsed.scheme, f"{user}:***@{host}", parsed.path, "", "")
    )


def latest_head_key(repository: str, pull_number: int) -> str:
    return f"{KEY_PREFIX}latest_head:{repository}#{pull_number}"


def attempts_key(message_id: str) -> str:
    return f"{KEY_PREFIX}attempts:{message_id}"


def ensure_consumer_group(client: redis.Redis) -> None:
    try:
        client.xgroup_create(STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
    except redis.ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def parse_job(fields: dict[str, str]) -> dict[str, Any]:
    """stream field 는 전부 문자열이므로 pull_number 만 정수로 되돌린다."""
    repository = (fields.get("repository") or "").strip()
    if not repository:
        # 여기서 막지 않으면 run_review_job 의 resolve_github_token 까지 가서야 터진다.
        # 그 경로는 재시도 대상이라 복구 불가능한 job 이 한도까지 반복된다.
        raise ValueError("job is missing repository")
    try:
        pull_number = int(fields.get("pull_number", ""))
    except ValueError as exc:
        raise ValueError(f"invalid pull_number in job: {fields.get('pull_number')!r}") from exc
    return {
        "v": fields.get("v", ""),
        "delivery_id": fields.get("delivery_id", ""),
        "action": fields.get("action", ""),
        "repository": repository,
        "pull_number": pull_number,
        "head_sha": fields.get("head_sha", ""),
        "enqueued_at": fields.get("enqueued_at", ""),
    }


def current_head(client: redis.Redis, repository: str, pull_number: int) -> str | None:
    return client.get(latest_head_key(repository, pull_number))


def is_stale(client: redis.Redis, job: dict[str, Any]) -> bool:
    """이 job 보다 새 push 가 이미 들어왔는지 판정한다.

    job 에 head_sha 가 없으면 판정 근거가 없으므로 stale 로 보지 않는다 (처리한다).
    latest_head 가 없으면 (TTL 만료 등) 역시 막지 않는다 — 리뷰를 빠뜨리는 쪽이
    중복 리뷰보다 나쁘다.
    """
    head_sha = job.get("head_sha") or ""
    if not head_sha:
        return False
    latest = current_head(client, job["repository"], job["pull_number"])
    if not latest:
        return False
    return latest != head_sha


def read_own_pending(
    client: redis.Redis,
    consumer: str,
    *,
    count: int = 10,
) -> list[tuple[str, dict[str, str]]]:
    """이 consumer 에게 이미 할당됐지만 ACK 하지 못한 job 을 가져온다 ('0').

    '>' 로만 읽으면 자기가 물고 있던 job 을 스스로 재개하지 못하고, min_idle_time
    (기본 30분) 뒤 XAUTOCLAIM 으로 회수될 때까지 방치된다. 워커가 재기동되는 흔한
    경우에 30분 지연이 그대로 발생하므로 기동 직후 자기 PEL 을 먼저 훑는다.
    """
    response = client.xreadgroup(
        CONSUMER_GROUP,
        consumer,
        {STREAM_KEY: "0"},
        count=count,
    )
    if not response:
        return []
    _, entries = response[0]
    return [entry for entry in entries if entry and entry[1]]


def read_new_jobs(
    client: redis.Redis,
    consumer: str,
    *,
    count: int = 1,
    block_ms: int = 5000,
) -> list[tuple[str, dict[str, str]]]:
    """아직 아무도 안 집어간 job 을 가져온다 ('>')."""
    response = client.xreadgroup(
        CONSUMER_GROUP,
        consumer,
        {STREAM_KEY: ">"},
        count=count,
        block=block_ms,
    )
    if not response:
        return []
    _, entries = response[0]
    return entries


def claim_abandoned_jobs(
    client: redis.Redis,
    consumer: str,
    *,
    min_idle_ms: int = DEFAULT_RECLAIM_IDLE_MS,
    count: int = 1,
) -> list[tuple[str, dict[str, str]]]:
    """죽은 워커가 물고 있던 job 을 회수한다.

    XREADGROUP 으로 집어든 뒤 ACK 전에 프로세스가 죽으면 그 job 은 PEL(Pending
    Entries List) 에 남는다. redelivery 로는 안 잡히는 구간이라 이 회수 루프가
    유일한 복구 경로다.
    """
    # cursor 를 무시하고 매번 0-0 에서만 시작하면, 아직 idle 이 아닌 항목이 스캔
    # 한도(COUNT 의 약 10배) 이상 앞에 쌓였을 때 뒤쪽의 버려진 job 에 영영 닿지
    # 못한다. cursor 가 0-0 으로 돌아올 때까지 이어서 훑는다.
    #
    # 스트림에서 이미 지워진 메시지(result[2])는 따로 ACK 할 필요가 없다. Redis 가
    # XAUTOCLAIM 시점에 PEL 에서 제거한 뒤 보고용으로만 돌려준다.
    claimed: list[tuple[str, dict[str, str]]] = []
    cursor = "0-0"
    for _ in range(MAX_RECLAIM_SCANS):
        result = client.xautoclaim(
            STREAM_KEY,
            CONSUMER_GROUP,
            consumer,
            min_idle_time=min_idle_ms,
            start_id=cursor,
            count=count,
        )
        entries = result[1] if len(result) >= 2 else []
        claimed.extend(entry for entry in entries if entry and entry[1])
        cursor = result[0] if result else "0-0"
        if not cursor or cursor == "0-0" or len(claimed) >= count:
            break
    return claimed


def record_attempt(client: redis.Redis, message_id: str) -> int:
    """INCR 과 EXPIRE 를 한 왕복으로 묶는다.

    나눠 보내면 INCR 직후 장애가 났을 때 TTL 없는 키가 영구히 남는다.
    """
    key = attempts_key(message_id)
    pipe = client.pipeline()
    pipe.incr(key)
    pipe.expire(key, ATTEMPTS_TTL_SECONDS)
    count, _ = pipe.execute()
    return int(count)


def clear_attempts(client: redis.Redis, message_id: str) -> None:
    client.delete(attempts_key(message_id))


def ack(client: redis.Redis, message_id: str) -> None:
    client.xack(STREAM_KEY, CONSUMER_GROUP, message_id)
    clear_attempts(client, message_id)


def send_to_dead_letter(
    client: redis.Redis,
    message_id: str,
    fields: dict[str, str],
    reason: str,
) -> None:
    """복구 불가 job 을 별도 stream 에 남기고 원본은 ACK 한다.

    ACK 하지 않으면 XAUTOCLAIM 이 같은 job 을 영원히 다시 집어온다.
    """
    payload = dict(fields)
    payload["dead_reason"] = reason
    payload["original_message_id"] = message_id
    # maxlen 없이 두면 복구 불가능한 job 이 쌓여 Redis 메모리를 영구히 먹는다.
    client.xadd(DEAD_LETTER_KEY, payload, maxlen=DEAD_LETTER_MAXLEN, approximate=True)
    ack(client, message_id)
