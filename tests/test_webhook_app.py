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
