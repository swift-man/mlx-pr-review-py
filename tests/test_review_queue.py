"""worker 측 큐 계약 테스트.

가장 중요한 테스트는 맨 위의 계약 리터럴 고정이다. receiver repo
(pr-review-receiver/receiver/queue.py) 와 이름이 어긋나면 job 이 조용히 유실되는데,
런타임에서는 '큐가 비어 있다' 로만 보여서 원인을 찾기 어렵다.
"""

from __future__ import annotations

import unittest
from unittest import mock

import redis

from review_runner import review_queue, review_worker


class FakeRedis:
    """worker 가 쓰는 명령만 구현한 최소 대역."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.counters: dict[str, int] = {}
        self.acked: list[str] = []
        self.dead: list[dict[str, str]] = []
        self.fail_on: set[str] = set()
        self.own_pending: list = []
        self.new_jobs: list = []
        self.autoclaim_calls: list[str] = []
        self.autoclaim_counts: list[int] = []
        self.autoclaim_batches: list = []
        self.pipeline_executions = 0

    def _check(self, command: str) -> None:
        if command in self.fail_on:
            raise redis.ConnectionError(f"injected failure on {command}")

    def get(self, key: str) -> str | None:
        self._check("get")
        return self.store.get(key)

    def set(self, key: str, value: str, **_kwargs) -> bool:
        self.store[key] = value
        return True

    def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key: str, ttl: int) -> bool:
        return True

    def delete(self, key: str) -> int:
        self.counters.pop(key, None)
        return 1 if self.store.pop(key, None) is not None else 0

    def xack(self, stream: str, group: str, message_id: str) -> int:
        self.acked.append(message_id)
        return 1

    def xadd(self, stream: str, fields: dict[str, str], **kwargs) -> str:
        if stream == review_queue.DEAD_LETTER_KEY:
            entry = dict(fields)
            entry["_maxlen"] = str(kwargs.get("maxlen", ""))
            self.dead.append(entry)
        return "9-0"

    def pipeline(self):
        return _FakePipeline(self)

    def xreadgroup(self, group, consumer, streams, count=None, block=None):
        key = next(iter(streams))
        if streams[key] == "0":
            return [(key, list(self.own_pending))] if self.own_pending else []
        return [(key, list(self.new_jobs))] if self.new_jobs else []

    def xautoclaim(self, stream, group, consumer, min_idle_time=0, start_id="0-0", count=1):
        # cursor 를 따라가는지, 스캔 폭이 수집 목표와 분리됐는지 검증하기 위해 기록한다.
        self.autoclaim_calls.append(start_id)
        self.autoclaim_counts.append(count)
        batch = self.autoclaim_batches.pop(0) if self.autoclaim_batches else ("0-0", [], [])
        return batch


class _FakePipeline:
    """INCR/EXPIRE 를 모아 execute 에서 한 번에 적용한다."""

    def __init__(self, client: "FakeRedis") -> None:
        self.client = client
        self.ops: list = []

    def incr(self, key): self.ops.append(("incr", key)); return self
    def expire(self, key, ttl): self.ops.append(("expire", key, ttl)); return self

    def execute(self):
        out = []
        for op in self.ops:
            out.append(self.client.incr(op[1]) if op[0] == "incr" else True)
        self.client.pipeline_executions += 1
        return out

    def xgroup_create(self, *args, **kwargs) -> bool:
        return True


JOB_FIELDS = {
    "v": "1",
    "delivery_id": "d-1",
    "event": "pull_request",
    "action": "synchronize",
    "repository": "swift-man/demo",
    "pull_number": "7",
    "head_sha": "a" * 40,
    "enqueued_at": "1700000000.000",
}


class QueueContractTestCase(unittest.TestCase):
    def test_thresholds_have_a_single_source(self) -> None:
        """프롬프트와 런타임이 같은 객체를 봐야 값이 어긋날 수 없다.

        어긋나면 모델이 규칙대로 낸 지적을 런타임이 조용히 버리는데, 로그상
        '모델이 아무것도 안 냈다' 와 구분되지 않아 원인을 찾기 어렵다.
        """
        from review_runner import mlx_review_prompt, review_service, review_thresholds

        self.assertIs(mlx_review_prompt.MIN_COMMENT_CONFIDENCE,
                      review_thresholds.MIN_COMMENT_CONFIDENCE)
        self.assertIs(review_service.MIN_MODEL_COMMENT_CONFIDENCE,
                      review_thresholds.MIN_COMMENT_CONFIDENCE)
        self.assertIs(mlx_review_prompt.MIN_BLOCKING_CONFIDENCE,
                      review_thresholds.MIN_BLOCKING_CONFIDENCE)
        self.assertIs(review_service.MIN_BLOCKING_MODEL_COMMENT_CONFIDENCE,
                      review_thresholds.MIN_BLOCKING_CONFIDENCE)

    def test_contract_literals_are_pinned(self) -> None:
        """이 값들은 receiver repo 와 공유하는 계약이다. 바꾸려면 양쪽을 같이 바꿔야 한다."""
        self.assertEqual(review_queue.STREAM_KEY, "prr:review_jobs")
        self.assertEqual(review_queue.CONSUMER_GROUP, "prr:workers")
        self.assertEqual(review_queue.JOB_SCHEMA_VERSION, "1")
        self.assertEqual(review_queue.latest_head_key("o/r", 3), "prr:latest_head:o/r#3")
        # dead letter 도 모니터링 도구와 공유하는 키다.
        self.assertEqual(review_queue.DEAD_LETTER_KEY, "prr:review_dead")

    def test_parse_job_requires_repository(self) -> None:
        """repository 가 비면 resolve_github_token 까지 가서야 터져 재시도를 낭비한다."""
        with self.assertRaises(ValueError):
            review_queue.parse_job({**JOB_FIELDS, "repository": ""})

    def test_consumer_name_is_stable_across_restarts(self) -> None:
        """pid 를 넣으면 재기동마다 consumer 가 group 에 무한 누적된다."""
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("REVIEW_WORKER_NAME", None)
            first = review_worker.consumer_name()
            second = review_worker.consumer_name()
        self.assertEqual(first, second)
        self.assertNotIn(str(__import__("os").getpid()), first)

    def test_parse_job_rejects_non_positive_pull_number(self) -> None:
        """0 이나 음수는 재시도해도 복구되지 않으므로 즉시 격리한다."""
        for bad in ("0", "-3"):
            with self.assertRaises(ValueError):
                review_queue.parse_job({**JOB_FIELDS, "pull_number": bad})

    def test_read_new_jobs_filters_empty_entries(self) -> None:
        """빈 항목을 넘기면 process_message 가 repository 누락으로 오격리한다."""
        client = FakeRedis()
        client.new_jobs = [("1-0", {}), ("2-0", dict(JOB_FIELDS))]
        got = review_queue.read_new_jobs(client, "c1")
        self.assertEqual([mid for mid, _ in got], ["2-0"])

    def test_reclaim_follows_the_cursor(self) -> None:
        """cursor 를 무시하면 스캔 한도 뒤쪽의 버려진 job 에 영영 닿지 못한다."""
        client = FakeRedis()
        client.autoclaim_batches = [
            ("5-0", [], []),                       # 1차: idle 항목 없음, 더 볼 게 남음
            ("0-0", [("9-0", dict(JOB_FIELDS))], []),  # 2차: 뒤쪽에서 발견
        ]
        found = review_queue.claim_abandoned_jobs(client, "c1", count=1)
        self.assertEqual(client.autoclaim_calls, ["0-0", "5-0"], "반환된 cursor 로 이어서 훑어야 한다")
        self.assertEqual(len(found), 1)

    def test_parse_job_converts_pull_number_to_int(self) -> None:
        job = review_queue.parse_job(JOB_FIELDS)
        self.assertEqual(job["pull_number"], 7)
        self.assertIsInstance(job["pull_number"], int)
        self.assertEqual(job["repository"], "swift-man/demo")

    def test_parse_job_rejects_non_numeric_pull_number(self) -> None:
        with self.assertRaises(ValueError):
            review_queue.parse_job({**JOB_FIELDS, "pull_number": "not-a-number"})


class StartupResilienceTestCase(unittest.TestCase):
    """기동 시점 Redis 장애로 프로세스가 죽지 않아야 한다.

    죽으면 KeepAlive 가 곧바로 되살려 같은 지점에서 또 죽는 crashloop 가 된다.
    실제로 Thunderbolt 링크가 순간 끊겼을 때 'No route to host' 로 죽었다.
    """

    def test_worker_waits_instead_of_crashing_when_redis_is_down_at_startup(self) -> None:
        client = FakeRedis()
        calls = {"n": 0}

        def flaky_ensure(_c):
            calls["n"] += 1
            if calls["n"] == 1:
                raise redis.ConnectionError("No route to host")

        with mock.patch.object(review_worker, "_SHUTDOWN", False), \
             mock.patch.object(review_worker.review_queue, "build_redis_client", return_value=client), \
             mock.patch.object(review_worker.review_queue, "ensure_consumer_group", side_effect=flaky_ensure), \
             mock.patch.object(review_worker.review_queue, "read_own_pending", return_value=[]), \
             mock.patch.object(review_worker.review_queue, "claim_abandoned_jobs", side_effect=KeyboardInterrupt), \
             mock.patch.object(review_worker.time, "sleep"):
            try:
                review_worker.run_forever()
            except KeyboardInterrupt:
                pass  # 기동을 통과했다는 뜻 — 본 루프까지 도달

        self.assertGreaterEqual(calls["n"], 2, "첫 실패 후 재시도해야 한다")


class SanitizedRedisUrlTestCase(unittest.TestCase):
    """로그에 비밀번호가 새지 않는지 고정한다.

    LaunchAgent 는 /tmp 에 world-readable 로그를 만든다. 기동 로그 한 줄에
    REVIEW_REDIS_URL 을 그대로 찍으면 비밀번호가 평문으로 남는다.
    """

    def test_password_is_masked(self) -> None:
        masked = review_queue.sanitized_redis_url("redis://:s3cr3t@10.10.0.1:6379/0")
        self.assertNotIn("s3cr3t", masked)
        self.assertEqual(masked, "redis://:***@10.10.0.1:6379/0")

    def test_username_is_kept_but_password_masked(self) -> None:
        masked = review_queue.sanitized_redis_url("redis://worker:s3cr3t@h:6379/1")
        self.assertNotIn("s3cr3t", masked)
        self.assertIn("worker", masked)

    def test_url_without_password_is_unchanged(self) -> None:
        url = "redis://127.0.0.1:6379/0"
        self.assertEqual(review_queue.sanitized_redis_url(url), url)

    def test_query_and_fragment_are_dropped(self) -> None:
        """query 에 비밀번호를 실어보내는 구성도 있어 함께 제거한다."""
        masked = review_queue.sanitized_redis_url("redis://:p@h:6379/0?password=p2#frag")
        self.assertNotIn("p2", masked)
        self.assertNotIn("frag", masked)

    def test_query_only_password_is_not_leaked(self) -> None:
        """userinfo 가 없어도 query 의 비밀번호를 흘리면 안 된다.

        userinfo 유무로 일찍 반환하던 버전이 이 경로를 그대로 로그에 남겼다.
        redis-py 는 ``?password=`` 형식을 지원하므로 실제로 쓰일 수 있는 구성이다.
        """
        masked = review_queue.sanitized_redis_url("redis://h:6379/0?password=SECRET")
        self.assertNotIn("SECRET", masked)
        self.assertEqual(masked, "redis://h:6379/0")

    def test_lone_userinfo_token_is_masked(self) -> None:
        """콜론 없는 userinfo 는 사용자명인지 잘못 넣은 비밀번호인지 알 수 없다.

        redis-py 는 ``redis://tok3n@h`` 를 username 으로 파싱하지만(password 아님),
        그 자리에 비밀번호를 넣는 설정 실수가 흔하다. 구분할 수 없으므로 가린다.
        """
        masked = review_queue.sanitized_redis_url("redis://tok3n@h:6379/0")
        self.assertNotIn("tok3n", masked)
        self.assertEqual(masked, "redis://***@h:6379/0")

    def test_username_is_not_dropped_when_userinfo_exists(self) -> None:
        """userinfo 를 통째로 버리면 어느 계정으로 붙는지 알 수 없어진다."""
        masked = review_queue.sanitized_redis_url("redis://user:pw@h:6379/1")
        self.assertEqual(masked, "redis://user:***@h:6379/1")

    def test_ipv6_brackets_are_preserved(self) -> None:
        """parsed.hostname 은 대괄호를 벗겨내 ::1:6379 같은 잘못된 netloc 을 만든다."""
        masked = review_queue.sanitized_redis_url("redis://:pw@[::1]:6379/0")
        self.assertNotIn("pw", masked)
        self.assertEqual(masked, "redis://:***@[::1]:6379/0")

    def test_reclaim_scan_count_is_independent_of_collection_target(self) -> None:
        """XAUTOCLAIM 의 COUNT 는 반환 수가 아니라 PEL 스캔 한도다.

        수집 목표(count)를 그대로 넘기면 앞쪽에 idle 이 아닌 항목이 조금만 쌓여도
        뒤쪽의 죽은 job 에 닿지 못한다.
        """
        client = FakeRedis()
        client.autoclaim_batches = [("0-0", [("9-0", dict(JOB_FIELDS))], [])]
        review_queue.claim_abandoned_jobs(client, "c1", count=1)
        self.assertEqual(client.autoclaim_counts, [review_queue.RECLAIM_SCAN_COUNT])
class ShutdownOrderingTestCase(unittest.TestCase):
    """종료 요청 뒤에는 새 리뷰를 시작하지 않는다.

    순서가 반대면 SIGTERM 을 받고도 수 분 걸리는 리뷰를 시작해 launchd 의 종료
    유예(기본 20초)를 넘겨 SIGKILL 당한다.
    """

    def test_shutdown_is_checked_before_processing_a_new_job(self) -> None:
        client = FakeRedis()
        client.new_jobs = [("1-0", dict(JOB_FIELDS))]
        client.own_pending = [("9-0", dict(JOB_FIELDS))]
        processed: list[str] = []

        with mock.patch.object(review_worker, "_SHUTDOWN", True), \
             mock.patch.object(review_worker, "process_message",
                               side_effect=lambda _c, mid, _f: processed.append(mid)), \
             mock.patch.object(review_worker.review_queue, "build_redis_client", return_value=client), \
             mock.patch.object(review_worker.review_queue, "ensure_consumer_group"):
            review_worker.run_forever()

        self.assertEqual(processed, [], "종료 요청 상태에서는 어떤 job 도 시작하지 않아야 한다")


class StaleDetectionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeRedis()
        self.job = review_queue.parse_job(JOB_FIELDS)

    def test_matching_head_is_not_stale(self) -> None:
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "a" * 40
        self.assertFalse(review_queue.is_stale(self.client, self.job))

    def test_newer_head_makes_job_stale(self) -> None:
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "b" * 40
        self.assertTrue(review_queue.is_stale(self.client, self.job))

    def test_missing_latest_head_is_not_stale(self) -> None:
        """TTL 만료 등으로 기준이 없으면 막지 않는다 — 리뷰 누락이 중복보다 나쁘다."""
        self.assertFalse(review_queue.is_stale(self.client, self.job))

    def test_job_without_head_sha_is_not_stale(self) -> None:
        job = review_queue.parse_job({**JOB_FIELDS, "head_sha": ""})
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "b" * 40
        self.assertFalse(review_queue.is_stale(self.client, job))


class ProcessMessageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeRedis()
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "a" * 40

    def test_successful_review_is_acked(self) -> None:
        with mock.patch.object(review_worker, "run_review_job", return_value={"status": "completed"}) as run:
            review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        run.assert_called_once()
        self.assertEqual(self.client.acked, ["1-0"])
        self.assertEqual(self.client.dead, [])

    def test_stale_job_is_acked_without_running_the_model(self) -> None:
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "b" * 40
        with mock.patch.object(review_worker, "run_review_job") as run:
            review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        run.assert_not_called()
        self.assertEqual(self.client.acked, ["1-0"], "stale job 도 ACK 해야 PEL 에 쌓이지 않는다")

    def test_failed_review_is_left_pending_for_reclaim(self) -> None:
        with mock.patch.object(review_worker, "run_review_job", side_effect=RuntimeError("boom")):
            review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        self.assertEqual(self.client.acked, [], "실패한 job 을 ACK 하면 재시도 기회를 잃는다")
        self.assertEqual(self.client.dead, [])

    def test_last_attempt_failure_is_dead_lettered_with_the_real_error(self) -> None:
        """마지막 시도 실패는 즉시 격리하고 원인을 보존한다.

        ACK 없이 돌아가면 min_idle_ms 를 기다렸다가 다음 회수 주기에
        'max attempts exceeded' 라는 일반 문구로만 격리돼 실제 원인이 사라진다.
        """
        with mock.patch.dict("os.environ", {"REVIEW_WORKER_MAX_ATTEMPTS": "2"}):
            with mock.patch.object(review_worker, "run_review_job", side_effect=RuntimeError("boom")):
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)  # attempt 1
                self.assertEqual(self.client.dead, [], "아직 재시도 여지가 있다")
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)  # attempt 2 == limit

        self.assertEqual(len(self.client.dead), 1)
        reason = self.client.dead[0]["dead_reason"]
        self.assertIn("RuntimeError", reason)
        self.assertIn("boom", reason, "일반 문구가 아니라 실제 예외를 남겨야 한다")
        self.assertEqual(self.client.acked, ["1-0"], "격리 후에는 ACK 해야 무한 회수를 막는다")

    def test_dead_letter_is_size_capped(self) -> None:
        """maxlen 없이 두면 poison job 이 Redis 메모리를 영구히 먹는다."""
        review_worker.process_message(self.client, "1-0", {**JOB_FIELDS, "pull_number": "xx"})
        self.assertEqual(self.client.dead[0]["_maxlen"], str(review_queue.DEAD_LETTER_MAXLEN))

    def test_stale_check_runs_before_attempt_counter(self) -> None:
        """순서가 반대면 무효 job 이 dead letter 로 잘못 격리된다."""
        self.client.store[review_queue.latest_head_key("swift-man/demo", 7)] = "b" * 40
        with mock.patch.dict("os.environ", {"REVIEW_WORKER_MAX_ATTEMPTS": "1"}):
            with mock.patch.object(review_worker, "run_review_job") as run:
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        run.assert_not_called()
        self.assertEqual(self.client.dead, [], "stale 은 격리가 아니라 건너뛰기여야 한다")
        self.assertEqual(self.client.acked, ["1-0"])
        self.assertEqual(self.client.counters, {}, "stale job 은 재시도 카운터를 소모하지 않는다")

    def test_stale_check_failure_does_not_abort_the_job(self) -> None:
        """Redis 가 잠깐 안 될 때 리뷰를 포기하지 않는다."""
        self.client.fail_on.add("get")
        with mock.patch.object(review_worker, "run_review_job", return_value={"status": "completed"}) as run:
            review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        run.assert_called_once()
        self.assertEqual(self.client.acked, ["1-0"])

    def test_attempt_counter_uses_a_single_round_trip(self) -> None:
        """INCR 직후 장애 시 TTL 없는 키가 남지 않도록 파이프라인으로 묶는다."""
        with mock.patch.object(review_worker, "run_review_job", return_value={"status": "ok"}):
            review_worker.process_message(self.client, "1-0", JOB_FIELDS)
        self.assertEqual(self.client.pipeline_executions, 1)

    def test_dead_letter_releases_the_delivery_marker(self) -> None:
        """격리는 포기 선언이다. 마커가 남으면 재전송으로도 복구할 수 없다.

        실제로 PR #57 리뷰 job 이 격리된 뒤, 마커가 6.7일 남아 있어 GitHub
        redelivery 를 보내도 receiver 가 중복으로 걸러내는 상태였다.
        """
        marker = review_queue.delivery_marker_key("d-1")
        self.client.store[marker] = "claimed"
        review_worker.process_message(self.client, "1-0", {**JOB_FIELDS, "pull_number": "xx"})
        self.assertEqual(len(self.client.dead), 1)
        self.assertNotIn(marker, self.client.store, "격리 시 마커를 풀어야 재전송이 통한다")

    def test_malformed_job_goes_straight_to_dead_letter(self) -> None:
        with mock.patch.object(review_worker, "run_review_job") as run:
            review_worker.process_message(self.client, "1-0", {**JOB_FIELDS, "pull_number": "xx"})
        run.assert_not_called()
        self.assertEqual(len(self.client.dead), 1)
        self.assertIn("malformed", self.client.dead[0]["dead_reason"])
        self.assertEqual(self.client.acked, ["1-0"])

    def test_should_continue_survives_redis_outage(self) -> None:
        """리뷰 도중 Redis 가 끊겨도 진행 중인 리뷰를 중단시키지 않는다."""
        captured = {}

        def fake_review(**kwargs):
            captured["should_continue"] = kwargs["should_continue"]
            return {"status": "completed"}

        with mock.patch.object(review_worker, "resolve_github_token") as resolve:
            resolve.return_value = mock.Mock(token="t", source="test")
            with mock.patch.object(review_worker, "review_pull_request", side_effect=fake_review):
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)

        self.client.fail_on.add("get")
        self.assertTrue(captured["should_continue"](), "Redis 장애 시 중단이 아니라 계속 진행해야 한다")


if __name__ == "__main__":
    unittest.main()
