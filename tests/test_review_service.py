import os
import signal
import sys
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
import io
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

    def test_run_mlx_retries_with_cpu_after_native_abort_when_device_is_auto(self) -> None:
        failed = subprocess_result(
            stdout="",
            stderr="[METAL] Command buffer execution failed: Insufficient Memory",
            returncode=-signal.SIGABRT,
        )
        recovered = subprocess_result(
            stdout='{"summary":"ok","event":"COMMENT","positives":[],"concerns":[],"comments":[]}'
        )
        with mock.patch.dict(os.environ, {"MLX_REVIEW_CMD": "custom-client --json"}, clear=False):
            with mock.patch(
                "review_runner.review_service.subprocess.run",
                side_effect=[failed, recovered],
            ) as subprocess_run:
                result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(subprocess_run.call_count, 2)
        _, first_kwargs = subprocess_run.call_args_list[0]
        _, second_kwargs = subprocess_run.call_args_list[1]
        self.assertIsNone(first_kwargs.get("env"))
        self.assertEqual(second_kwargs["env"]["MLX_DEVICE"], "cpu")

    def test_run_mlx_does_not_override_explicit_gpu_setting_on_native_abort(self) -> None:
        completed = subprocess_result(
            stdout="",
            stderr="[METAL] Command buffer execution failed: Insufficient Memory",
            returncode=-signal.SIGABRT,
        )
        with mock.patch.dict(os.environ, {"MLX_REVIEW_CMD": "custom-client --json", "MLX_DEVICE": "gpu"}, clear=False):
            with mock.patch("review_runner.review_service.subprocess.run", return_value=completed) as subprocess_run:
                with self.assertRaisesRegex(RuntimeError, "MLX_DEVICE=cpu"):
                    review_service.run_mlx('{"repository":"demo"}')

        subprocess_run.assert_called_once()

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

    def test_validate_mlx_output_filters_positive_sentences_out_of_concerns(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="fortune/service.py",
            status="modified",
            patch='@@ -10,0 +10,1 @@\n+@dataclass\n',
            additions=1,
            deletions=0,
            right_side_lines={10},
        )

        validated = review_service.validate_mlx_output(
            {
                "summary": "운세 데이터 구조를 정리했습니다.",
                "event": "COMMENT",
                "positives": ["dataclass를 도입해 필드 계약이 한눈에 드러나고 초기화 보일러플레이트가 줄었습니다."],
                "concerns": [
                    "dataclass를 사용하여 코드의 가독성을 높였습니다.",
                    "새로운 테스트 파일이 추가되어 코드의 신뢰성을 높였습니다.",
                ],
                "comments": [],
            },
            [pr_file],
        )

        self.assertEqual(validated.concerns, [])
        self.assertEqual(validated.event, "COMMENT")
        self.assertEqual(
            validated.positives,
            ["dataclass를 도입해 필드 계약이 한눈에 드러나고 초기화 보일러플레이트가 줄었습니다."],
        )

    def test_validate_mlx_output_filters_identifier_localization_style_concerns(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="fortune/service.py",
            status="modified",
            patch='@@ -12,0 +12,1 @@\n+POSITIVE_CONCERN_MARKERS = ()\n',
            additions=1,
            deletions=0,
            right_side_lines={12},
        )

        validated = review_service.validate_mlx_output(
            {
                "summary": "구조를 정리했습니다.",
                "event": "COMMENT",
                "positives": ["캐시 관련 상수를 한곳에 모아 의도를 파악하기 쉬워졌습니다."],
                "concerns": ["POSITIVE_CONCERN_MARKERS는 영어로 작성되어 있습니다. 한국어로 변경해주세요."],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 12,
                        "body": "POSITIVE_CONCERN_MARKERS는 영어로 작성되어 있습니다. 한국어로 변경해주세요.",
                    }
                ],
            },
            [pr_file],
        )

        self.assertEqual(validated.concerns, [])
        self.assertEqual(validated.comments, [])

    def test_validate_mlx_output_logs_parser_and_validation_drop_stats(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="fortune/service.py",
            status="modified",
            patch='@@ -12,0 +12,1 @@\n+cache_entry = build_cache_entry()\n',
            additions=1,
            deletions=0,
            right_side_lines={12},
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            validated = review_service.validate_mlx_output(
                {
                    "_meta": {
                        "parse_mode": "salvaged_candidate",
                        "parse_error": "Expecting ',' delimiter",
                        "raw_comment_count": 4,
                        "normalized_comment_count": 2,
                        "dropped_comment_reasons": {"duplicate_comment": 1, "invalid_body": 1},
                    },
                    "summary": "캐시 구조를 정리했습니다.",
                    "event": "COMMENT",
                    "positives": ["캐시 생성 경로를 한곳으로 모아 흐름을 따라가기 쉬워졌습니다."],
                    "concerns": [],
                    "comments": [
                        {
                            "path": "fortune/service.py",
                            "line": 12,
                            "body": "캐시 생성 경로가 바뀌었으니 정상 흐름과 만료 흐름을 함께 검증하는 테스트를 추가해두는 편이 안전합니다.",
                        },
                        {
                            "path": "missing.py",
                            "line": 12,
                            "body": "이 파일은 PR에 없습니다.",
                        },
                        {
                            "path": "fortune/service.py",
                            "line": 12,
                            "body": "핵심 변경 의도가 diff 안에서 비교적 명확하게 드러납니다.",
                        },
                    ],
                },
                [pr_file],
                log_prefix="[delivery=test] ",
            )

        lines = stdout.getvalue().splitlines()
        self.assertIn(
            "[delivery=test] MLX parser parse_mode=salvaged_candidate raw_comments=4 normalized_comments=2 "
            "dropped_after_parse=duplicate_comment=1, invalid_body=1 parse_error=Expecting ',' delimiter",
            lines,
        )
        self.assertIn(
            "[delivery=test] Comment validation accepted_model_comments=1/3 rule_based_added=0 "
            "rule_based_duplicates=0 dropped_after_validation=path_mismatch=1, style_or_praise_only=1",
            lines,
        )
        self.assertEqual(len(validated.comments), 1)


def subprocess_result(*, stdout: str, stderr: str = "", returncode: int = 0) -> mock.Mock:
    completed = mock.Mock()
    completed.stdout = stdout
    completed.stderr = stderr
    completed.returncode = returncode
    return completed


if __name__ == "__main__":
    unittest.main()
