import os
import signal
import sys
import threading
import time
import types
import unittest
from unittest import mock

if "certifi" not in sys.modules:
    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/tmp/fake-cert.pem"
    sys.modules["certifi"] = fake_certifi

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.ModuleType("jwt")

from review_runner import review_service


class RunMlxTests(unittest.TestCase):
    def test_run_mlx_uses_inprocess_client_by_default(self) -> None:
        expected = {"summary": "ok"}
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MLX_REVIEW_CMD", None)
            with mock.patch("review_runner.mlx_review_client.review_payload", return_value=expected) as review_payload:
                with mock.patch("review_runner.review_service.subprocess.run") as subprocess_run:
                    result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result, expected)
        review_payload.assert_called_once_with({"repository": "demo"})
        subprocess_run.assert_not_called()

    def test_run_mlx_uses_inprocess_client_for_explicit_default_command(self) -> None:
        expected = {"summary": "ok"}
        command = f'"{sys.executable}" -m review_runner.mlx_review_client'
        with mock.patch.dict(os.environ, {"MLX_REVIEW_CMD": command}, clear=False):
            with mock.patch("review_runner.mlx_review_client.review_payload", return_value=expected) as review_payload:
                with mock.patch("review_runner.review_service.subprocess.run") as subprocess_run:
                    result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result, expected)
        review_payload.assert_called_once_with({"repository": "demo"})
        subprocess_run.assert_not_called()

    def test_run_mlx_uses_subprocess_for_custom_command(self) -> None:
        completed = subprocess_result(
            stdout='{"summary":"ok","event":"COMMENT","positives":[],"concerns":[],"comments":[]}'
        )
        with mock.patch.dict(os.environ, {"MLX_REVIEW_CMD": "custom-client --json"}, clear=False):
            with mock.patch("review_runner.review_service.subprocess.run", return_value=completed) as subprocess_run:
                with mock.patch("review_runner.mlx_review_client.review_payload") as review_payload:
                    result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result["summary"], "ok")
        subprocess_run.assert_called_once()
        review_payload.assert_not_called()

    def test_run_mlx_surfaces_native_abort_hint_for_sigabrt_subprocess(self) -> None:
        completed = subprocess_result(stdout="", stderr="abort() called", returncode=-signal.SIGABRT)
        with mock.patch.dict(os.environ, {"MLX_REVIEW_CMD": "custom-client --json"}, clear=False):
            with mock.patch("review_runner.review_service.subprocess.run", return_value=completed):
                with self.assertRaisesRegex(RuntimeError, "MLX_DEVICE=cpu"):
                    review_service.run_mlx('{"repository":"demo"}')

    def test_run_mlx_serializes_concurrent_reviews(self) -> None:
        entered_first = threading.Event()
        release_first = threading.Event()
        entered_second = threading.Event()
        counter_lock = threading.Lock()
        active_calls = 0
        max_active_calls = 0
        started_calls = 0

        def fake_review_payload(payload: dict[str, str]) -> dict[str, str]:
            nonlocal active_calls, max_active_calls, started_calls
            with counter_lock:
                started_calls += 1
                call_number = started_calls
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            if call_number == 1:
                entered_first.set()
                release_first.wait(timeout=2)
            else:
                entered_second.set()
            with counter_lock:
                active_calls -= 1
            return {"summary": payload["id"]}

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MLX_REVIEW_CMD", None)
            with mock.patch("review_runner.mlx_review_client.review_payload", side_effect=fake_review_payload):
                results: list[dict[str, str]] = []

                def invoke(prompt_id: str) -> None:
                    results.append(review_service.run_mlx(f'{{"id":"{prompt_id}"}}'))

                first = threading.Thread(target=invoke, args=("first",))
                second = threading.Thread(target=invoke, args=("second",))

                first.start()
                self.assertTrue(entered_first.wait(timeout=1))
                second.start()
                time.sleep(0.1)
                self.assertFalse(entered_second.is_set())
                release_first.set()
                first.join(timeout=2)
                second.join(timeout=2)

        self.assertEqual(max_active_calls, 1)
        self.assertCountEqual(results, [{"summary": "first"}, {"summary": "second"}])


class ReviewNormalizationTests(unittest.TestCase):
    def test_detect_secret_logging_emits_one_comment_per_file(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="price_proxy/dbsec.py",
            status="modified",
            patch=(
                "@@ -0,0 +218,3 @@\n"
                '+print(f"access token={token}")\n'
                '+logger.info("secret=%s", secret)\n'
                '+logging.warning("api_key=%s", api_key)\n'
            ),
            additions=3,
            deletions=0,
            right_side_lines={218, 219, 220},
        )

        comments = review_service.detect_secret_logging(pr_file)

        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].path, "price_proxy/dbsec.py")
        self.assertEqual(comments[0].line, 218)
        self.assertIn("여러 곳", comments[0].body)

    def test_validate_mlx_output_rewrites_no_findings_summary_when_findings_exist(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="price_proxy/dbsec.py",
            status="modified",
            patch='@@ -0,0 +218,1 @@\n+print(f"access token={token}")\n',
            additions=1,
            deletions=0,
            right_side_lines={218},
        )

        validated = review_service.validate_mlx_output(
            {
                "summary": review_service.DEFAULT_NO_FINDINGS_SUMMARY,
                "event": "COMMENT",
                "positives": [],
                "concerns": [],
                "comments": [],
            },
            [pr_file],
        )

        self.assertEqual(validated.summary, review_service.DEFAULT_FINDINGS_SUMMARY)
        self.assertEqual(validated.event, "REQUEST_CHANGES")
        self.assertEqual(len(validated.comments), 1)


def subprocess_result(*, stdout: str, stderr: str = "", returncode: int = 0) -> mock.Mock:
    completed = mock.Mock()
    completed.stdout = stdout
    completed.stderr = stderr
    completed.returncode = returncode
    return completed


if __name__ == "__main__":
    unittest.main()
