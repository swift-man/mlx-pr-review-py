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
        # cursor 를 따라가는지 검증하기 위해 호출 인자를 기록한다.
        self.autoclaim_calls.append(start_id)
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
