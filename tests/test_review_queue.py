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

    def xadd(self, stream: str, fields: dict[str, str]) -> str:
        if stream == review_queue.DEAD_LETTER_KEY:
            self.dead.append(dict(fields))
        return "9-0"

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

    def test_parse_job_converts_pull_number_to_int(self) -> None:
        job = review_queue.parse_job(JOB_FIELDS)
        self.assertEqual(job["pull_number"], 7)
        self.assertIsInstance(job["pull_number"], int)
        self.assertEqual(job["repository"], "swift-man/demo")

    def test_parse_job_rejects_non_numeric_pull_number(self) -> None:
        with self.assertRaises(ValueError):
            review_queue.parse_job({**JOB_FIELDS, "pull_number": "not-a-number"})


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

    def test_job_goes_to_dead_letter_after_max_attempts(self) -> None:
        with mock.patch.dict("os.environ", {"REVIEW_WORKER_MAX_ATTEMPTS": "2"}):
            with mock.patch.object(review_worker, "run_review_job", side_effect=RuntimeError("boom")):
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)  # attempt 1
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)  # attempt 2
                self.assertEqual(self.client.dead, [])
                review_worker.process_message(self.client, "1-0", JOB_FIELDS)  # attempt 3 > 2

        self.assertEqual(len(self.client.dead), 1)
        self.assertIn("max attempts", self.client.dead[0]["dead_reason"])
        self.assertEqual(self.client.acked, ["1-0"], "dead letter 로 보낸 뒤에는 ACK 해야 무한 회수를 막는다")

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
