import io
import json
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

if "certifi" not in sys.modules:
    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/tmp/fake-cert.pem"
    sys.modules["certifi"] = fake_certifi

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.ModuleType("jwt")

if "fastapi" not in sys.modules:
    fake_fastapi = types.ModuleType("fastapi")

    class FakeFastAPI:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def post(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    class FakeHTTPException(Exception):
        def __init__(self, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fake_fastapi.BackgroundTasks = object
    fake_fastapi.FastAPI = FakeFastAPI
    fake_fastapi.HTTPException = FakeHTTPException
    fake_fastapi.Request = object
    sys.modules["fastapi"] = fake_fastapi

from review_runner import webhook_app


class HandlePullRequestEventTests(unittest.TestCase):
    def setUp(self) -> None:
        with webhook_app._LATEST_DELIVERY_LOCK:
            webhook_app._LATEST_DELIVERY_SEQUENCE = 0
            webhook_app._LATEST_PULL_REQUEST_DELIVERIES.clear()

    def test_delivery_registry_marks_only_latest_pr_delivery_current(self) -> None:
        first = webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-1")
        second = webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-2")

        self.assertFalse(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, first))
        self.assertTrue(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, second))

        webhook_app.clear_pull_request_delivery("demo/repo", 7, first)
        self.assertTrue(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, second))

        webhook_app.clear_pull_request_delivery("demo/repo", 7, second)
        self.assertFalse(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, second))

    def test_delivery_registry_ignores_duplicate_active_head(self) -> None:
        first = webhook_app.register_pull_request_delivery_result("demo/repo", 7, "delivery-1", "abc123")
        duplicate = webhook_app.register_pull_request_delivery_result("demo/repo", 7, "delivery-2", "abc123")

        self.assertTrue(first.accepted)
        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.marker, first.marker)
        self.assertIn("Duplicate delivery for active PR head abc123", duplicate.reason)
        self.assertTrue(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, first.marker))

    def test_delivery_registry_accepts_new_head(self) -> None:
        first = webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-1", "abc123")
        second = webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-2", "def456")

        self.assertFalse(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, first))
        self.assertTrue(webhook_app.is_latest_pull_request_delivery("demo/repo", 7, second))

    def test_handle_pull_request_event_passes_superseded_delivery_check(self) -> None:
        first = webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-1")
        webhook_app.register_pull_request_delivery("demo/repo", 7, "delivery-2")
        auth = mock.Mock(token="token-123", source="github_app_installation")

        def fake_review_pull_request(**kwargs):
            self.assertFalse(kwargs["should_continue"]())
            return {
                "status": "skipped",
                "repository": kwargs["repository"],
                "pull_number": kwargs["pull_number"],
                "reason": "superseded",
            }

        stdout = io.StringIO()
        with mock.patch("review_runner.webhook_app.resolve_github_token", return_value=auth):
            with mock.patch("review_runner.webhook_app.review_pull_request", side_effect=fake_review_pull_request):
                with mock.patch("review_runner.webhook_app.time.monotonic", side_effect=[10.0, 11.0]):
                    with redirect_stdout(stdout):
                        webhook_app.handle_pull_request_event("demo/repo", 7, "delivery-1", first)

        lines = stdout.getvalue().splitlines()
        payload = json.loads(lines[3].removeprefix("[delivery=delivery-1] "))
        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["reason"], "superseded")

    def test_handle_pull_request_event_logs_structured_auth_failure(self) -> None:
        stdout = io.StringIO()
        with mock.patch("review_runner.webhook_app.resolve_github_token", side_effect=RuntimeError("bad auth")):
            with mock.patch("review_runner.webhook_app.time.monotonic", side_effect=[100.0, 103.0]):
                with redirect_stdout(stdout):
                    webhook_app.handle_pull_request_event("demo/repo", 7, "delivery-1")

        lines = stdout.getvalue().splitlines()
        self.assertIn("[delivery=delivery-1] Starting review for demo/repo#7", lines[0])
        self.assertIn("Review failed in 3.0s during auth_resolution: bad auth", lines[1])
        payload = json.loads(lines[2].removeprefix("[delivery=delivery-1] "))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["stage"], "auth_resolution")
        self.assertEqual(payload["error"], "bad auth")
        self.assertIsNone(payload["auth_source"])

    def test_handle_pull_request_event_logs_structured_review_failure(self) -> None:
        auth = mock.Mock(token="token-123", source="github_app_installation")
        stdout = io.StringIO()
        with mock.patch("review_runner.webhook_app.resolve_github_token", return_value=auth):
            with mock.patch("review_runner.webhook_app.review_pull_request", side_effect=RuntimeError("mlx exploded")):
                with mock.patch("review_runner.webhook_app.time.monotonic", side_effect=[200.0, 204.5]):
                    with redirect_stdout(stdout):
                        webhook_app.handle_pull_request_event("demo/repo", 8, "delivery-2")

        lines = stdout.getvalue().splitlines()
        self.assertIn("Resolved GitHub auth via github_app_installation", lines[1])
        self.assertIn("Review failed in 4.5s during review_execution: mlx exploded", lines[2])
        payload = json.loads(lines[3].removeprefix("[delivery=delivery-2] "))
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["stage"], "review_execution")
        self.assertEqual(payload["error"], "mlx exploded")
        self.assertEqual(payload["auth_source"], "github_app_installation")

    def test_handle_pull_request_event_keeps_success_logging_shape(self) -> None:
        auth = mock.Mock(token="token-123", source="personal_access_token")
        result = {"status": "completed", "repository": "demo/repo", "pull_number": 9}
        stdout = io.StringIO()
        with mock.patch("review_runner.webhook_app.resolve_github_token", return_value=auth):
            with mock.patch("review_runner.webhook_app.review_pull_request", return_value=result):
                with mock.patch("review_runner.webhook_app.time.monotonic", side_effect=[10.0, 12.0]):
                    with redirect_stdout(stdout):
                        webhook_app.handle_pull_request_event("demo/repo", 9, "delivery-3")

        lines = stdout.getvalue().splitlines()
        self.assertIn("Review finished in 2.0s", lines[2])
        payload = json.loads(lines[3].removeprefix("[delivery=delivery-3] "))
        self.assertEqual(payload, result)


if __name__ == "__main__":
    unittest.main()
