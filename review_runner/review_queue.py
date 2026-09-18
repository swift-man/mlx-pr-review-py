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

# XAUTOCLAIM 한 번이 훑을 PEL 엔트리 수. 반환 개수 상한이 아니다.
RECLAIM_SCAN_COUNT = 100

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

    비밀번호가 실릴 수 있는 경로가 둘이라 양쪽 다 막는다:

    1. userinfo (``redis://:pw@host``)
    2. query (``redis://host/0?password=pw``) — redis-py 가 이 형식도 지원한다.

    userinfo 만 보고 일찍 반환하면 (2) 가 그대로 샌다. 실제로 그렇게 새던 버전이
    있었으므로, query/fragment 는 비밀번호 유무와 무관하게 **항상** 제거한다.
    """
    raw = url or redis_url()
    parsed = urllib.parse.urlsplit(raw)
    has_userinfo = "@" in parsed.netloc

    # 가릴 것도 지울 것도 없으면 원본 그대로 둔다.
    if not has_userinfo and not parsed.query and not parsed.fragment:
        return raw

    # host:port 는 netloc 원본에서 잘라 쓴다. parsed.hostname 은 IPv6 의 대괄호를
    # 벗겨내기 때문에(::1), 포트와 이어 붙이면 ::1:6379 같은 잘못된 netloc 이 된다.
    host = parsed.netloc.rpartition("@")[-1]

    if not has_userinfo:
        netloc = host
    elif parsed.password is not None:
        # user:pw@ — 콜론으로 나뉘어 있으니 앞쪽은 사용자명이 분명하다. 사용자명은
        # 비밀이 아니고 어느 계정으로 붙는지가 진단에 도움이 되므로 남긴다.
        netloc = f"{parsed.username or ''}:***@{host}"
    else:
        # token@ — 콜론이 없다. redis-py 는 이걸 username 으로 파싱하지만
        # (password 아님), 그 자리에 비밀번호를 잘못 넣는 설정 실수가 흔하다.
        # 사용자명인지 새어나가면 안 되는 값인지 여기서는 구분할 수 없으므로
        # 가리는 쪽을 택한다. 로그에 사용자명이 안 보이는 손해보다 비밀이 새는
        # 손해가 크다.
        netloc = f"***@{host}"

    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


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
    if pull_number <= 0:
        # GitHub PR 번호는 1 이상이다. 0 이나 음수는 재시도해도 복구되지 않으므로
        # 여기서 막아 즉시 dead letter 로 보낸다.
        raise ValueError(f"pull_number must be positive, got {pull_number}")
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
    # MAXLEN/XDEL 로 정리된 빈 항목이 섞여 올 수 있다. 그대로 넘기면 process_message
    # 가 repository 누락으로 보고 dead letter 로 오격리한다. 다른 읽기 경로와
    # 동일하게 걸러낸다.
    return [entry for entry in entries if entry and entry[1]]


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
            # XAUTOCLAIM 의 COUNT 는 '돌려줄 개수' 가 아니라 'PEL 을 몇 개까지
            # 훑을지' 다. 여기에 수집 목표(count=1)를 그대로 넘기면 앞쪽에 아직
            # idle 이 아닌 항목이 조금만 쌓여도 뒤쪽 job 에 닿지 못한다. 스캔 폭은
            # 넉넉히 주고, 수집 목표는 아래 파이썬 루프에서 끊는다.
            count=RECLAIM_SCAN_COUNT,
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


def delivery_marker_key(delivery_id: str) -> str:
    """receiver 의 중복 제거 마커. 키 형식은 receiver repo 와 공유하는 계약이다."""
    return f"{KEY_PREFIX}delivery:{delivery_id}"


def send_to_dead_letter(
    client: redis.Redis,
    message_id: str,
    fields: dict[str, str],
    reason: str,
) -> None:
    """복구 불가 job 을 별도 stream 에 남기고 원본은 ACK 한다.

    ACK 하지 않으면 XAUTOCLAIM 이 같은 job 을 영원히 다시 집어온다.

    함께 delivery 마커도 지운다. 격리는 '이 job 을 포기했다' 는 뜻인데, 마커가
    남아 있으면 운영자가 GitHub 에서 같은 delivery 를 재전송해도 receiver 가
    '이미 처리됨' 으로 걸러내 복구할 길이 막힌다. receiver 가 enqueue 실패 시
    선점을 되돌리는 것과 같은 이유다.
    """
    payload = dict(fields)
    payload["dead_reason"] = reason
    payload["original_message_id"] = message_id
    # maxlen 없이 두면 복구 불가능한 job 이 쌓여 Redis 메모리를 영구히 먹는다.
    client.xadd(DEAD_LETTER_KEY, payload, maxlen=DEAD_LETTER_MAXLEN, approximate=True)

    delivery_id = fields.get("delivery_id") or ""
    if delivery_id:
        try:
            client.delete(delivery_marker_key(delivery_id))
        except redis.RedisError:
            # 격리 자체는 끝났다. 마커는 TTL 로 만료되므로 여기서 실패해도
            # 최악의 경우 재전송이 그때까지 막힐 뿐이다.
            pass

    ack(client, message_id)
