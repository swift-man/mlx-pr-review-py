import io
import json
import os
import signal
import sys
import threading
import time
import types
import unittest
from contextlib import redirect_stdout
from typing import Any
from unittest import mock

if "certifi" not in sys.modules:
    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/tmp/fake-cert.pem"
    sys.modules["certifi"] = fake_certifi

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.ModuleType("jwt")

from review_runner import mlx_review_parser, review_service


def _mlx_env(**overrides: str) -> dict[str, str]:
    env = {
        "MLX_REVIEW_BACKEND": "",
        "MLX_GENERATE_URL": "",
    }
    env.update(overrides)
    return env


def _finding_body(
    *,
    problem: str = "검증 가능한 문제가 있습니다.",
    why: str = "현재 코드 경로에서 사용자-visible 영향이 발생합니다.",
    fix: str = "해당 경로를 명시적으로 처리하세요.",
    confidence: str = "High",
) -> str:
    return f"Problem: {problem} Why it matters: {why} Suggested fix: {fix} Confidence: {confidence}"


class RunMlxTests(unittest.TestCase):
    def test_configured_mlx_backend_defaults_to_local(self) -> None:
        with mock.patch.dict(os.environ, _mlx_env(), clear=False):
            self.assertEqual(review_service.configured_mlx_backend(), "local")

    def test_configured_mlx_backend_accepts_explicit_remote(self) -> None:
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_BACKEND="remote"), clear=False):
            self.assertEqual(review_service.configured_mlx_backend(), "remote")

    def test_configured_mlx_backend_accepts_explicit_local(self) -> None:
        with mock.patch.dict(
            os.environ,
            _mlx_env(MLX_REVIEW_BACKEND="local", MLX_GENERATE_URL="http://127.0.0.1:8002/v1/generate"),
            clear=False,
        ):
            self.assertEqual(review_service.configured_mlx_backend(), "local")

    def test_configured_mlx_backend_uses_generate_url_as_remote_hint(self) -> None:
        with mock.patch.dict(
            os.environ,
            _mlx_env(MLX_GENERATE_URL="http://127.0.0.1:8002/v1/generate"),
            clear=False,
        ):
            self.assertEqual(review_service.configured_mlx_backend(), "remote")

    def test_configured_mlx_backend_rejects_invalid_value(self) -> None:
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_BACKEND="sidecar"), clear=False):
            with self.assertRaisesRegex(RuntimeError, "local, remote"):
                review_service.configured_mlx_backend()

    def test_run_mlx_uses_inprocess_client_by_default(self) -> None:
        expected = {"summary": "ok"}
        with mock.patch.dict(os.environ, _mlx_env(), clear=False):
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
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_CMD=command), clear=False):
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
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_CMD="custom-client --json"), clear=False):
            with mock.patch("review_runner.review_service.subprocess.run", return_value=completed) as subprocess_run:
                with mock.patch("review_runner.mlx_review_client.review_payload") as review_payload:
                    result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result["summary"], "ok")
        subprocess_run.assert_called_once()
        review_payload.assert_not_called()

    def test_run_mlx_uses_remote_client_when_configured(self) -> None:
        expected = {"summary": "remote"}
        with mock.patch.dict(
            os.environ,
            _mlx_env(MLX_REVIEW_BACKEND="remote", MLX_REVIEW_CMD="custom-client --json"),
            clear=False,
        ):
            with mock.patch("review_runner.mlx_remote_review_client.review_payload", return_value=expected) as remote_payload:
                with mock.patch("review_runner.review_service.subprocess.run") as subprocess_run:
                    with mock.patch("review_runner.mlx_review_client.review_payload") as local_payload:
                        result = review_service.run_mlx('{"repository":"demo"}')

        self.assertEqual(result, expected)
        remote_payload.assert_called_once_with({"repository": "demo"})
        subprocess_run.assert_not_called()
        local_payload.assert_not_called()

    def test_run_mlx_surfaces_native_abort_hint_for_sigabrt_subprocess(self) -> None:
        completed = subprocess_result(stdout="", stderr="abort() called", returncode=-signal.SIGABRT)
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_CMD="custom-client --json"), clear=False):
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
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_CMD="custom-client --json"), clear=False):
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
        with mock.patch.dict(os.environ, _mlx_env(MLX_REVIEW_CMD="custom-client --json", MLX_DEVICE="gpu"), clear=False):
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

        with mock.patch.dict(os.environ, _mlx_env(), clear=False):
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

        # 긍정 어투("가독성을 높였습니다") 로 concerns 에 실려온 항목은 legacy_concerns 경로로
        # 통과하더라도 sanitize_text_items 가 looks_like_positive_only_concern 으로 drop 한다.
        # 결과적으로 must_fix / suggestions / comments 가 모두 비어 '지적 없음' 상태가 되므로
        # 최종 event 는 APPROVE 로 승격된다 (Phase 3: APPROVE 지원).
        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.event, "APPROVE")
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

        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.comments, [])

    def test_validate_mlx_output_filters_process_policy_comments(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="review_runner/mlx_review_prompt.py",
            status="modified",
            patch='@@ -20,0 +20,1 @@\n+"PR 제목과 description 은 한글로 작성합니다."\n',
            additions=1,
            deletions=0,
            right_side_lines={20},
        )

        validated = review_service.validate_mlx_output(
            {
                "summary": "프롬프트 구성을 정리했습니다.",
                "event": "COMMENT",
                "positives": ["프롬프트 규칙을 모듈로 분리해 유지보수 경계를 더 분명하게 만들었습니다."],
                "concerns": ["PR 제목과 description이 한국어로 작성되어야 하며, 리뷰 텍스트는 작업 흐름을 분석해야 합니다."],
                "comments": [
                    {
                        "path": "review_runner/mlx_review_prompt.py",
                        "line": 20,
                        "body": "PR 제목과 description이 한국어로 작성되어야 하며, 리뷰 텍스트는 작업 흐름을 분석해야 합니다.",
                    }
                ],
            },
            [pr_file],
        )

        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
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
                            "confidence": 0.91,
                            "body": _finding_body(
                                problem="캐시 생성 경로 변경에 대한 검증이 필요합니다.",
                                why="만료 흐름 회귀를 놓칠 수 있습니다.",
                                fix="정상 흐름과 만료 흐름 테스트를 추가하세요.",
                            ),
                        },
                        {
                            "path": "missing.py",
                            "line": 12,
                            "body": _finding_body(
                                problem="PR에 없는 파일입니다.",
                                why="GitHub 라인 코멘트 등록이 실패합니다.",
                                fix="실제 diff 파일만 사용하세요.",
                            ),
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


class ReviewBotConfigTests(unittest.TestCase):
    def _pr_file(self, filename: str) -> "review_service.PullRequestFile":
        return review_service.PullRequestFile(
            filename=filename,
            status="modified",
            patch="@@ -0,0 +1,1 @@\n+x\n",
            additions=1,
            deletions=0,
            right_side_lines={1},
        )

    def test_parse_reviewbot_config_reads_review_lists(self) -> None:
        raw_config = """
version: 1

review: # reviewbot settings
  include: # allow source files
    - "**/*.swift"
    - "Package.swift"
  exclude:
    # generated files
    - "**/*.generated.swift"
    - README.md # docs
  always_review:
    - ".reviewbot.yml"
    - "AGENTS.md"
"""

        config = review_service.parse_reviewbot_config(raw_config)

        self.assertTrue(config.loaded)
        self.assertEqual(config.include, ("**/*.swift", "Package.swift"))
        self.assertEqual(config.exclude, ("**/*.generated.swift", "README.md"))
        self.assertEqual(config.always_review, (".reviewbot.yml", "AGENTS.md"))

    def test_parse_reviewbot_config_ignores_unknown_list_buckets(self) -> None:
        raw_config = """
version: 1
review:
  labels:
    - "review-heavy"
  profiles:
    ios:
      include:
        - "docs/**"
      exclude:
        - "Sources/**"
  include:
    - "**/*.swift"
"""

        config = review_service.parse_reviewbot_config(raw_config)

        self.assertEqual(config.include, ("**/*.swift",))
        self.assertEqual(config.exclude, ())
        self.assertEqual(config.always_review, ())

    def test_parse_reviewbot_config_rejects_unsupported_known_bucket_values(self) -> None:
        cases = (
            'review:\n  include: ["Sources/App.swift"]\n',
            'review:\n  exclude: "README.md"\n',
            'review:\n  always_review: .reviewbot.yml\n',
        )
        for raw_config in cases:
            with self.subTest(raw_config=raw_config):
                with self.assertRaisesRegex(ValueError, "unsupported value for review"):
                    review_service.parse_reviewbot_config(raw_config)

    def test_parse_reviewbot_config_rejects_unterminated_quoted_values(self) -> None:
        raw_config = """
version: 1
review:
  include:
    - "Sources/App.swift
"""

        with self.assertRaisesRegex(ValueError, "unterminated"):
            review_service.parse_reviewbot_config(raw_config)

    def test_filter_reviewbot_files_applies_include_exclude_and_always_review(self) -> None:
        config = review_service.ReviewBotConfig(
            include=("**/*.swift", "Package.swift"),
            exclude=("Pods/**", "**/*.generated.swift", "**/*.md"),
            always_review=(".reviewbot.yml", "Package.swift", "README.md"),
            loaded=True,
        )
        files = [
            self._pr_file("Sources/App.swift"),
            self._pr_file("RootFile.swift"),
            self._pr_file("Pods/Generated.swift"),
            self._pr_file("Sources/API.generated.swift"),
            self._pr_file("README.md"),
            self._pr_file(".reviewbot.yml"),
            self._pr_file("Package.swift"),
        ]

        filtered, skipped = review_service.filter_reviewbot_files(files, config)

        self.assertEqual(
            [pr_file.filename for pr_file in filtered],
            ["Sources/App.swift", "RootFile.swift", "README.md", ".reviewbot.yml", "Package.swift"],
        )
        self.assertEqual(skipped, 2)

    def test_filter_reviewbot_files_forces_control_files_even_when_config_excludes_them(self) -> None:
        config = review_service.ReviewBotConfig(
            include=(),
            exclude=("**/*",),
            always_review=(),
            loaded=True,
        )
        files = [
            self._pr_file(".reviewbot.yml"),
            self._pr_file("AGENTS.md"),
            self._pr_file("Sources/App.swift"),
        ]

        filtered, skipped = review_service.filter_reviewbot_files(files, config)

        self.assertEqual([pr_file.filename for pr_file in filtered], [".reviewbot.yml", "AGENTS.md"])
        self.assertEqual(skipped, 1)

    def test_reviewbot_glob_matching_uses_path_segments(self) -> None:
        self.assertTrue(review_service.reviewbot_glob_matches("**/*.swift", "RootFile.swift"))
        self.assertTrue(review_service.reviewbot_glob_matches("**/*.swift", "Sources/App.swift"))
        self.assertTrue(review_service.reviewbot_glob_matches("**/*.xcassets/**", "Assets.xcassets/icon.png"))
        self.assertTrue(review_service.reviewbot_glob_matches("Tuist/**/*.swift", "Tuist/Project.swift"))
        self.assertTrue(review_service.reviewbot_glob_matches("*.md", "README.md"))
        self.assertFalse(review_service.reviewbot_glob_matches("*.md", "docs/guide.md"))
        self.assertFalse(review_service.reviewbot_glob_matches(".reviewbot.yml", "nested/.reviewbot.yml"))

    def test_load_patchable_pr_files_result_fetches_config_from_pr_head(self) -> None:
        raw_config = """
version: 1
review:
  include:
    - "**/*.swift"
    - "Package.swift"
  exclude:
    - "Pods/**"
    - "**/*.md"
  always_review:
    - ".reviewbot.yml"
"""
        case = self

        class FakeGitHub:
            repository = "swift-man/app"

            def __init__(self) -> None:
                self.loaded_paths: list[tuple[str, str]] = []

            def list_pr_files(self, pull_number: int) -> list[dict[str, Any]]:
                case.assertEqual(pull_number, 7)
                return [
                    self._raw_file("Sources/App.swift"),
                    self._raw_file("README.md"),
                    self._raw_file("Pods/Lib.swift"),
                    self._raw_file(".reviewbot.yml"),
                    self._raw_file("Package.swift"),
                ]

            def _raw_file(self, filename: str) -> dict[str, Any]:
                return {
                    "filename": filename,
                    "status": "modified",
                    "patch": "@@ -0,0 +1,1 @@\n+x\n",
                    "additions": 1,
                    "deletions": 0,
                }

            def get_pull_head_sha(self, pull_number: int) -> str:
                case.assertEqual(pull_number, 7)
                return "abc123"

            def get_file_text(self, path: str, *, ref: str) -> str:
                self.loaded_paths.append((path, ref))
                return raw_config

        fake_github = FakeGitHub()

        result = review_service.load_patchable_pr_files_result(fake_github, 7)

        self.assertEqual(fake_github.loaded_paths, [(review_service.REVIEWBOT_CONFIG_PATH, "abc123")])
        self.assertEqual(
            [pr_file.filename for pr_file in result.files],
            ["Sources/App.swift", ".reviewbot.yml", "Package.swift"],
        )
        self.assertEqual(result.patchable_count, 5)
        self.assertEqual(result.skipped_by_reviewbot, 2)
        self.assertTrue(result.reviewbot_config_loaded)

    def test_load_patchable_pr_files_result_reviews_all_files_when_config_is_missing(self) -> None:
        case = self

        class FakeGitHub:
            repository = "swift-man/app"

            def __init__(self) -> None:
                self.loaded_paths: list[tuple[str, str]] = []

            def list_pr_files(self, pull_number: int) -> list[dict[str, Any]]:
                case.assertEqual(pull_number, 7)
                return [
                    {
                        "filename": "README.md",
                        "status": "modified",
                        "patch": "@@ -0,0 +1,1 @@\n+x\n",
                        "additions": 1,
                        "deletions": 0,
                    }
                ]

            def get_pull_head_sha(self, pull_number: int) -> str:
                case.assertEqual(pull_number, 7)
                return "abc123"

            def get_file_text(self, path: str, *, ref: str) -> str:
                self.loaded_paths.append((path, ref))
                raise RuntimeError("GitHub API GET https://api.github.com/repos/swift-man/app/contents/.reviewbot.yml failed: 404 Not Found")

        fake_github = FakeGitHub()
        result = review_service.load_patchable_pr_files_result(fake_github, 7)

        self.assertEqual(fake_github.loaded_paths, [(review_service.REVIEWBOT_CONFIG_PATH, "abc123")])
        self.assertEqual([pr_file.filename for pr_file in result.files], ["README.md"])
        self.assertEqual(result.patchable_count, 1)
        self.assertEqual(result.skipped_by_reviewbot, 0)
        self.assertFalse(result.reviewbot_config_loaded)


class NormalizeSeverityTests(unittest.TestCase):
    def test_recognizes_all_four_severities_case_insensitively(self) -> None:
        cases = {
            "Blocking": review_service.SEVERITY_BLOCKING,
            "blocking": review_service.SEVERITY_BLOCKING,
            "Critical": review_service.SEVERITY_BLOCKING,
            "critical": review_service.SEVERITY_BLOCKING,
            "CRITICAL": review_service.SEVERITY_BLOCKING,
            "  Major ": review_service.SEVERITY_MAJOR,
            "MINOR": review_service.SEVERITY_MINOR,
            "suggestion": review_service.SEVERITY_SUGGESTION,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(review_service.normalize_severity(raw), expected)

    def test_defaults_to_minor_when_missing_or_unknown(self) -> None:
        # 모델이 누락하거나 완전히 엉뚱한 값을 보내도 Blocking 으로 튀지 않도록 Minor 폴백.
        # 'blocker' / 'high' 같이 흔히 쓰는 관용어는 별도 매핑으로 흡수되므로 여기서는
        # 매핑에 명시적으로 없는 값들만 검사한다.
        for raw in (None, "", "urgent", "cosmetic", "p0", "wishlist", 3, ["Blocking"]):
            with self.subTest(raw=raw):
                self.assertEqual(review_service.normalize_severity(raw), review_service.SEVERITY_MINOR)


class NormalizeConfidenceTests(unittest.TestCase):
    def test_accepts_numeric_confidence_values(self) -> None:
        self.assertEqual(review_service.normalize_confidence(0.8), 0.8)
        self.assertEqual(review_service.normalize_confidence("0.93"), 0.93)
        self.assertEqual(review_service.normalize_confidence(1), 1.0)

    def test_rejects_missing_bool_and_out_of_range_values(self) -> None:
        for raw in (None, True, False, "high", -0.1, 1.1):
            with self.subTest(raw=raw):
                self.assertIsNone(review_service.normalize_confidence(raw))


class SeverityRoutingAndRenderingTests(unittest.TestCase):
    def _make_pr_file(self) -> "review_service.PullRequestFile":
        return review_service.PullRequestFile(
            filename="fortune/service.py",
            status="modified",
            patch="@@ -0,0 +1,1 @@\n+x = 1\n",
            additions=1,
            deletions=0,
            right_side_lines={1},
        )

    def test_blocking_comment_drives_request_changes_and_major_too(self) -> None:
        for severity in ("Blocking", "Major"):
            with self.subTest(severity=severity):
                validated = review_service.validate_mlx_output(
                    {
                        "summary": "로직 변경.",
                        "event": "COMMENT",
                        "positives": [],
                        "must_fix": [],
                        "suggestions": [],
                        "comments": [
                            {
                                "path": "fortune/service.py",
                                "line": 1,
                                "severity": severity,
                                "confidence": 0.9,
                                "body": _finding_body(
                                    problem="위험한 상태 전이가 검증 없이 남습니다.",
                                    why="현재 코드에서 잘못된 리뷰 이벤트가 발생합니다.",
                                    fix="해당 상태 전이를 검증하거나 코멘트를 제거하세요.",
                                ),
                            }
                        ],
                    },
                    [self._make_pr_file()],
                )
                self.assertEqual(validated.event, "REQUEST_CHANGES")
                self.assertIn("위험한 상태 전이가 검증 없이 남습니다.", validated.must_fix)
                self.assertEqual(len(validated.comments), 1)
                self.assertEqual(validated.comments[0].severity, review_service.normalize_severity(severity))

    def test_minor_comment_alone_stays_at_comment_level(self) -> None:
        # Minor/Suggestion 만 있는 경우에는 REQUEST_CHANGES 가 발동하면 안 된다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "네이밍 정리.",
                "event": "REQUEST_CHANGES",  # 모델이 요청해도 다운그레이드돼야 함
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Minor",
                        "confidence": 0.9,
                        "body": _finding_body(
                            problem="낮은 위험의 검증 누락입니다.",
                            why="특정 입력에서 결과가 달라질 수 있습니다.",
                            fix="경계 조건 테스트를 추가하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.event, "COMMENT")
        self.assertEqual(validated.must_fix, [])
        self.assertIn("낮은 위험의 검증 누락입니다.", validated.suggestions)
        self.assertEqual(len(validated.comments), 1)
        self.assertEqual(validated.comments[0].severity, "Minor")

    def test_model_comment_missing_confidence_is_dropped(self) -> None:
        validated = review_service.validate_mlx_output(
            {
                "summary": "검토.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "body": _finding_body(
                            problem="근거가 부족합니다.",
                            why="잘못된 리뷰가 남습니다.",
                            fix="제거하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.comments, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_model_comment_below_confidence_threshold_is_dropped(self) -> None:
        validated = review_service.validate_mlx_output(
            {
                "summary": "검토.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.79,
                        "body": _finding_body(
                            problem="확신이 낮습니다.",
                            why="false positive 입니다.",
                            fix="제거하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.comments, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_model_comment_without_required_sections_is_dropped(self) -> None:
        comments, stats = review_service.collect_validated_comments(
            {
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.95,
                        "body": "이 줄은 문제가 있으니 수정해야 합니다.",
                    }
                ]
            },
            [self._make_pr_file()],
        )

        self.assertEqual(comments, [])
        self.assertEqual(
            stats.dropped_model_comment_reasons,
            {"missing_required_finding_sections": 1},
        )

    def test_model_comment_with_numeric_confidence_label_is_dropped(self) -> None:
        comments, stats = review_service.collect_validated_comments(
            {
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.95,
                        "body": (
                            "Problem: 잘못된 상태입니다. Why it matters: 요청이 실패합니다. "
                            "Suggested fix: 검증을 추가하세요. Confidence: 0.95"
                        ),
                    }
                ]
            },
            [self._make_pr_file()],
        )

        self.assertEqual(comments, [])
        self.assertEqual(stats.dropped_model_comment_reasons, {"invalid_confidence_label": 1})

    def test_model_comment_with_confidence_phrase_before_final_section_is_dropped(self) -> None:
        comments, stats = review_service.collect_validated_comments(
            {
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.95,
                        "body": (
                            "Problem: 설명 중 Confidence: High 예시를 언급합니다. "
                            "Why it matters: 요청이 실패합니다. Suggested fix: 검증을 추가하세요."
                        ),
                    }
                ]
            },
            [self._make_pr_file()],
        )

        self.assertEqual(comments, [])
        self.assertEqual(stats.dropped_model_comment_reasons, {"missing_required_finding_sections": 1})

    def test_blocking_or_major_without_high_confidence_label_is_dropped(self) -> None:
        for severity in ("Blocking", "Major"):
            with self.subTest(severity=severity):
                comments, stats = review_service.collect_validated_comments(
                    {
                        "comments": [
                            {
                                "path": "fortune/service.py",
                                "line": 1,
                                "severity": severity,
                                "confidence": 0.95,
                                "body": _finding_body(
                                    problem="실패 경로가 있습니다.",
                                    why="현재 코드에서 사용자 요청이 실패합니다.",
                                    fix="실패 조건을 처리하세요.",
                                    confidence="Medium",
                                ),
                            }
                        ]
                    },
                    [self._make_pr_file()],
                )

                self.assertEqual(comments, [])
                self.assertEqual(
                    stats.dropped_model_comment_reasons,
                    {"blocking_without_high_confidence": 1},
                )

    def test_structured_comment_with_positive_fix_phrase_is_kept(self) -> None:
        comments, stats = review_service.collect_validated_comments(
            {
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.95,
                        "body": _finding_body(
                            problem="None 반환 시 AttributeError가 발생합니다.",
                            why="요청이 500으로 끝납니다.",
                            fix="None 분기를 먼저 처리하면 회귀 방지에 도움이 됩니다.",
                        ),
                    }
                ]
            },
            [self._make_pr_file()],
        )

        self.assertEqual(len(comments), 1)
        self.assertEqual(stats.dropped_model_comment_reasons, {})

    def test_non_object_model_comment_is_dropped_without_crashing(self) -> None:
        comments, stats = review_service.collect_validated_comments(
            {"comments": ["이 값은 dict가 아닙니다."]},
            [self._make_pr_file()],
        )

        self.assertEqual(comments, [])
        self.assertEqual(stats.dropped_model_comment_reasons, {"non_object_comment": 1})

    def test_model_comment_rejects_bool_zero_and_float_lines(self) -> None:
        valid_body = _finding_body(
            problem="잘못된 라인 타입입니다.",
            why="GitHub 라인 코멘트가 엉뚱한 줄에 붙을 수 있습니다.",
            fix="정수 라인만 허용하세요.",
        )
        for raw_line in (True, False, 0, 1.5):
            with self.subTest(raw_line=raw_line):
                comments, stats = review_service.collect_validated_comments(
                    {
                        "comments": [
                            {
                                "path": "fortune/service.py",
                                "line": raw_line,
                                "severity": "Major",
                                "confidence": 0.95,
                                "body": valid_body,
                            }
                        ]
                    },
                    [self._make_pr_file()],
                )

                self.assertEqual(comments, [])
                self.assertEqual(stats.dropped_model_comment_reasons, {"invalid_line_type": 1})

    def test_rule_based_secret_logging_comment_gets_blocking_severity(self) -> None:
        # 비밀값 로그 감지는 직접 Blocking 을 붙이므로 모델이 아무것도 안 보내도
        # event 가 REQUEST_CHANGES 로 승격해야 한다.
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
                "summary": "토큰 출력을 점검합니다.",
                "event": "COMMENT",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [],
            },
            [pr_file],
        )
        self.assertEqual(len(validated.comments), 1)
        comment = validated.comments[0]
        self.assertEqual(comment.severity, review_service.SEVERITY_CRITICAL)
        self.assertTrue(review_service.has_required_finding_sections(comment.body))
        self.assertEqual(review_service.extract_confidence_label(comment.body), "high")
        self.assertEqual(validated.event, "REQUEST_CHANGES")
        payload = review_service.build_review_payload(
            summary=validated.summary,
            event=validated.event,
            comments=validated.comments,
            positives=validated.positives,
            must_fix=validated.must_fix,
            suggestions=validated.suggestions,
        )
        rendered_body = payload["comments"][0]["body"]
        self.assertIn("Problem:", rendered_body)
        self.assertIn("Why it matters:", rendered_body)
        self.assertIn("Suggested fix:", rendered_body)
        self.assertIn("Confidence: High", rendered_body)
        self.assertIn("Confidence score: 1.00", rendered_body)

    def test_rule_based_comments_use_required_finding_template(self) -> None:
        files = [
            review_service.PullRequestFile(
                filename="hooks.py",
                status="modified",
                patch="@@ -10,1 +10,2 @@\n if not signature:\n+    return\n",
                additions=1,
                deletions=0,
                right_side_lines={11},
            ),
            review_service.PullRequestFile(
                filename="price_proxy/dbsec.py",
                status="modified",
                patch='@@ -0,0 +218,1 @@\n+print(f"access token={token}")\n',
                additions=1,
                deletions=0,
                right_side_lines={218},
            ),
            review_service.PullRequestFile(
                filename="api/response.py",
                status="modified",
                patch='@@ -0,0 +5,1 @@\n+return {"stauts": "ok"}\n',
                additions=1,
                deletions=0,
                right_side_lines={5},
            ),
        ]

        comments = review_service.detect_rule_based_comments(files)

        self.assertEqual(len(comments), 3)
        for comment in comments:
            with self.subTest(path=comment.path, line=comment.line):
                self.assertTrue(review_service.has_required_finding_sections(comment.body))
                self.assertEqual(review_service.extract_confidence_label(comment.body), "high")

    def test_rule_based_detectors_ignore_test_fixture_patch_strings(self) -> None:
        pr_file = review_service.PullRequestFile(
            filename="tests/test_review_service.py",
            status="modified",
            patch=(
                "@@ -790,0 +790,2 @@\n"
                "+patch='@@ -0,0 +218,1 @@\\n+print(f\"access token={token}\")\\n'\n"
                "+patch='@@ -0,0 +5,1 @@\\n+return {\"stauts\": \"ok\"}\\n'\n"
            ),
            additions=2,
            deletions=0,
            right_side_lines={790, 791},
        )

        self.assertEqual(review_service.detect_rule_based_comments([pr_file]), [])

    def test_build_review_payload_prefixes_line_comment_body_with_severity_tag(self) -> None:
        payload = review_service.build_review_payload(
            summary="요약",
            event="REQUEST_CHANGES",
            comments=[
                review_service.ReviewComment(
                    path="fortune/service.py",
                    line=10,
                    body="서명 검증이 제거돼 인증 우회 위험이 있습니다.",
                    severity=review_service.SEVERITY_CRITICAL,
                ),
                review_service.ReviewComment(
                    path="fortune/service.py",
                    line=20,
                    body="네이밍을 payload_meta 로 통일하면 일관성이 좋아집니다.",
                    severity=review_service.SEVERITY_MINOR,
                ),
            ],
            positives=[],
            must_fix=[],
            suggestions=[],
        )
        rendered_bodies = [c["body"] for c in payload["comments"]]
        self.assertEqual(
            rendered_bodies,
            [
                "[Blocking] 서명 검증이 제거돼 인증 우회 위험이 있습니다.\n\nConfidence score: 1.00",
                "[Minor] 네이밍을 payload_meta 로 통일하면 일관성이 좋아집니다.\n\nConfidence score: 1.00",
            ],
        )

    def test_severity_survives_full_parser_to_validator_pipeline(self) -> None:
        """파서가 severity 를 drop 하면 모델 생성 라인 코멘트가 모두 Minor 로 강등되는
        실전 회귀를 막는 E2E 테스트. raw MLX JSON 문자열부터 validate_mlx_output 까지
        실제 운영 경로를 그대로 타야 한다."""
        raw_output = json.dumps(
            {
                "summary": "인증 우회 가능성을 점검합니다.",
                "event": "COMMENT",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Blocking",
                        "confidence": 0.95,
                        "body": _finding_body(
                            problem="인증 우회 경로가 생깁니다.",
                            why="서명 없는 요청이 처리될 수 있습니다.",
                            fix="서명 검증을 복구하세요.",
                        ),
                    }
                ],
            }
        )
        parsed, _meta = mlx_review_parser.parse_and_normalize_model_output(raw_output)
        validated = review_service.validate_mlx_output(parsed, [self._make_pr_file()])

        self.assertEqual(len(validated.comments), 1)
        self.assertEqual(
            validated.comments[0].severity, review_service.SEVERITY_CRITICAL
        )
        self.assertIn("인증 우회 경로가 생깁니다.", validated.must_fix)
        self.assertEqual(validated.event, "REQUEST_CHANGES")

    def test_normalize_severity_accepts_common_synonyms(self) -> None:
        # 코드 리뷰 관용어도 4단계로 안전하게 흡수한다.
        cases = {
            "blocker": review_service.SEVERITY_CRITICAL,
            "severe": review_service.SEVERITY_CRITICAL,
            "high": review_service.SEVERITY_MAJOR,
            "medium": review_service.SEVERITY_MINOR,
            "low": review_service.SEVERITY_SUGGESTION,
            "nit": review_service.SEVERITY_SUGGESTION,
            "nitpick": review_service.SEVERITY_SUGGESTION,
            "optional": review_service.SEVERITY_SUGGESTION,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(review_service.normalize_severity(raw), expected)

    def test_no_findings_at_all_results_in_approve_event(self) -> None:
        # must_fix / suggestions / comments 모두 비어 있으면 명시적 승인(APPROVE) 으로
        # 올려야 한다. 모델이 COMMENT 로 emit 해도 런타임이 APPROVE 로 승격한다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "이 PR 은 안정적으로 보입니다.",
                "event": "COMMENT",  # 의도적으로 낮은 값 - 런타임이 올려야 함
                "positives": ["캐시 계약을 분명히 한 부분이 돋보입니다."],
                "must_fix": [],
                "suggestions": [],
                "comments": [],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.event, "APPROVE")
        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.comments, [])

    def test_top_level_suggestions_without_line_confidence_are_ignored(self) -> None:
        # 모델이 path/line/confidence 없는 전역 suggestions 만 보내면 finding 으로
        # 게시하지 않는다. false positive 방지 쪽이 optional suggestion 누락보다 중요하다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "구조를 정리했습니다.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": [],
                "suggestions": ["네이밍을 payload_meta 로 통일하면 일관성이 좋아집니다."],
                "comments": [],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.event, "APPROVE")
        self.assertEqual(validated.suggestions, [])

    def test_minor_line_comment_alone_stays_at_comment_not_approve(self) -> None:
        # Minor 라인 코멘트가 하나라도 있으면 APPROVE 가 아니라 COMMENT. 라인 코멘트가
        # 비어 있어야 APPROVE 조건이 충족된다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "네이밍 정리.",
                "event": "APPROVE",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Minor",
                        "confidence": 0.9,
                        "body": _finding_body(
                            problem="낮은 위험의 검증 누락입니다.",
                            why="특정 입력에서 결과가 달라질 수 있습니다.",
                            fix="경계 조건 테스트를 추가하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.event, "COMMENT")
        self.assertEqual(len(validated.comments), 1)

    def test_top_level_must_fix_without_line_confidence_is_ignored(self) -> None:
        # 모델이 APPROVE 를 보내든 REQUEST_CHANGES 를 보내든, 검증된 라인 코멘트 없는
        # raw must_fix 는 merge-blocking 신호로 쓰지 않는다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "보안 체크를 확인해야 합니다.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": ["signature 검증이 제거되어 인증 우회 위험이 있습니다."],
                "suggestions": [],
                "comments": [],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_unknown_severity_value_from_model_falls_back_to_minor(self) -> None:
        # 모델이 매핑에 없는 비표준 등급(p0, urgent 등) 을 보내도 Minor 로 안전 폴백.
        # blocker/high 같은 일반 관용어는 이미 Blocking/Major 로 매핑되므로 여기서는
        # 정말로 사전에 없는 문자열을 사용한다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "테스트.",
                "event": "COMMENT",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "p0",
                        "confidence": 0.9,
                        "body": _finding_body(
                            problem="낮은 위험의 검증 누락입니다.",
                            why="특정 입력에서 결과가 달라질 수 있습니다.",
                            fix="경계 조건 테스트를 추가하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )
        self.assertEqual(len(validated.comments), 1)
        self.assertEqual(validated.comments[0].severity, review_service.SEVERITY_MINOR)
        # Minor 로 폴백됐으니 REQUEST_CHANGES 가 발동하면 안 된다.
        self.assertEqual(validated.event, "COMMENT")


class BuildReviewResultTests(unittest.TestCase):
    def _make_validated_review(self, **overrides: Any) -> review_service.ValidatedReview:
        defaults = dict(
            comments=[],
            summary="요약",
            event="COMMENT",
            positives=["개선된 점 예시"],
            must_fix=[],
            suggestions=[],
        )
        defaults.update(overrides)
        return review_service.ValidatedReview(**defaults)

    def test_build_review_result_returns_severity_aware_counts(self) -> None:
        # Phase 2 이후 ValidatedReview 는 concerns 가 없다. 이 경로가 AttributeError
        # 없이 must_fix / suggestions 기반 카운트를 돌려주는지 고정한다. 이 회귀가
        # 풀리면 실제 운영에서 review_execution 단계에서 17분짜리 리뷰가 통째로 날아간다.
        review = self._make_validated_review(
            must_fix=["signature 검증 우회 위험이 있습니다."],
            suggestions=["네이밍을 통일하면 좋습니다.", "로그 레벨을 낮추세요."],
        )
        result = review_service.build_review_result(
            repository="owner/repo",
            pull_number=1,
            validated_review=review,
            payload={"body": "...", "event": "COMMENT", "comments": []},
            auth_source="github_app_installation",
        )
        self.assertEqual(result["must_fix_count"], 1)
        self.assertEqual(result["suggestion_count"], 2)
        # 하위 호환: concern_count 는 여전히 노출되며 must_fix + suggestions 합.
        self.assertEqual(result["concern_count"], 3)
        self.assertEqual(result["auth_source"], "github_app_installation")
        self.assertEqual(result["status"], "completed")

    def test_build_review_result_handles_empty_buckets(self) -> None:
        review = self._make_validated_review()
        result = review_service.build_review_result(
            repository="owner/repo",
            pull_number=2,
            validated_review=review,
            payload={"body": "...", "event": "COMMENT", "comments": []},
            auth_source=None,
        )
        self.assertEqual(result["must_fix_count"], 0)
        self.assertEqual(result["suggestion_count"], 0)
        self.assertEqual(result["concern_count"], 0)
        self.assertEqual(result["auth_source"], "personal_access_token")


class DedupeAcrossSectionsTests(unittest.TestCase):
    def test_summarize_structured_comment_uses_problem_section(self) -> None:
        summaries = review_service.summarize_comment_bodies(
            [
                review_service.ReviewComment(
                    path="a.py",
                    line=1,
                    body=_finding_body(
                        problem="None 반환 시 AttributeError가 발생합니다.",
                        why="요청이 500으로 끝납니다.",
                        fix="None 분기를 먼저 처리하면 회귀 방지에 도움이 됩니다.",
                    ),
                    severity=review_service.SEVERITY_MAJOR,
                )
            ]
        )

        self.assertEqual(summaries, ["None 반환 시 AttributeError가 발생합니다."])

    def test_identical_finding_in_must_fix_and_comment_is_deduped_to_comment(self) -> None:
        # 같은 finding 이 must_fix 와 comments[] 에 동시에 있으면 라인 anchor 쪽을 보존.
        same_text = "signature 검증이 제거되어 인증 우회 위험이 있습니다."
        must_fix, suggestions, comments, positives = review_service.dedupe_across_sections(
            must_fix=[same_text],
            suggestions=[],
            comments=[
                review_service.ReviewComment(
                    path="a.py",
                    line=1,
                    body=same_text,
                    severity=review_service.SEVERITY_CRITICAL,
                )
            ],
            positives=[],
        )
        self.assertEqual(must_fix, [])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].body, same_text)

    def test_similar_wording_across_sections_is_deduped(self) -> None:
        # 유사도 기반이라 문장이 미세하게 달라도 같은 finding 으로 판정해 하위 섹션에서 drop.
        must_fix, suggestions, comments, positives = review_service.dedupe_across_sections(
            must_fix=["npm 릴리즈 워크플로우는 새로운 패키지의 출시를 자동화하는 데 도움이 될 것입니다."],
            suggestions=[],
            comments=[
                review_service.ReviewComment(
                    path=".github/workflows/npm-publish.yml",
                    line=1,
                    body="npm 릴리즈 워크플로우는 새로운 패키지의 출시를 자동화하는 데 도움이 될 것입니다.",
                    severity=review_service.SEVERITY_MAJOR,
                )
            ],
            positives=["npm 릴리즈 워크플로우가 추가되었습니다."],
        )
        # 우선순위: comments > must_fix > positives. 라인 코멘트만 보존.
        self.assertEqual(len(comments), 1)
        self.assertEqual(must_fix, [])
        # positives 는 동사형이 달라 (추가되었습니다 vs 도움이 될 것입니다) Jaccard 가
        # 임계치에 못 미칠 수 있으므로 보존 여부에 대한 assert 를 걸지 않는다.

    def test_distinct_findings_are_all_preserved(self) -> None:
        must_fix, suggestions, comments, positives = review_service.dedupe_across_sections(
            must_fix=["signature 검증이 제거되어 인증 우회 위험이 있습니다."],
            suggestions=["네이밍을 payload_meta 로 통일하면 좋습니다."],
            comments=[
                review_service.ReviewComment(
                    path="a.py",
                    line=1,
                    body="SQL 인젝션에 취약한 쿼리 문자열 조합입니다.",
                    severity=review_service.SEVERITY_MAJOR,
                )
            ],
            positives=["dataclass 를 도입해 계약이 명확해졌습니다."],
        )
        # 서로 다른 주제는 dedup 대상이 아님. 전부 보존.
        self.assertEqual(len(must_fix), 1)
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(len(comments), 1)
        self.assertEqual(len(positives), 1)

    def test_empty_or_whitespace_text_does_not_crash_dedup(self) -> None:
        # 매우 짧은 문장은 토큰이 거의 없어 판정 불가 → 보존.
        must_fix, suggestions, comments, positives = review_service.dedupe_across_sections(
            must_fix=["a"],
            suggestions=["b"],
            comments=[],
            positives=[""],
        )
        self.assertEqual(must_fix, ["a"])
        self.assertEqual(suggestions, ["b"])


class DescriptionPatternEndToEndTests(unittest.TestCase):
    """패턴 4 (description 재진술 라인 코멘트) 전체 파이프라인 회귀 방지.

    별도의 severity 강등 함수 없이도 looks_like_praise_only_comment (POSITIVE_CONCERN_
    MARKERS / DESCRIPTIVE_NARRATION_SUFFIXES 등을 모두 사용) 가
    collect_validated_comments 단에서 코멘트를 drop 하므로, '도움이 될 것입니다' 류
    Major 라인 코멘트는 자동으로 사라진다.
    """

    def test_description_style_major_line_comment_is_dropped_at_praise_filter(self) -> None:
        # 실제 패턴 4 재현: '도움이 될 것입니다' 가 POSITIVE_CONCERN_MARKERS 의 '도움이 될'
        # 에 걸려 looks_like_positive_only_concern → looks_like_praise_only_comment 가
        # True → collect_validated_comments 가 코멘트를 drop. 결과적으로 라인 코멘트 0건.
        pr_file = review_service.PullRequestFile(
            filename=".github/workflows/npm-publish.yml",
            status="added",
            patch="@@ -0,0 +1,1 @@\n+on: push\n",
            additions=1,
            deletions=0,
            right_side_lines={1},
        )
        validated = review_service.validate_mlx_output(
            {
                "summary": "npm 릴리즈 자동화.",
                "event": "COMMENT",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": ".github/workflows/npm-publish.yml",
                        "line": 1,
                        "severity": "Major",
                        "body": "npm 릴리즈 워크플로우는 새로운 패키지의 출시를 자동화하는 데 도움이 될 것입니다.",
                    }
                ],
            },
            [pr_file],
        )
        # 코멘트가 전부 drop 되고 must_fix / suggestions 도 비어 있으면 APPROVE 까지
        # 자동 승격 (Phase 2 의 APPROVE 로직). REQUEST_CHANGES 가 과발동하던 패턴 4
        # 사례가 정반대로 깨끗하게 정리됨.
        self.assertEqual(validated.comments, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_major_with_english_exception_name_is_preserved(self) -> None:
        # narration / 긍정 매처에 걸리지 않는 정당한 Major (영문 예외명 사용) 는
        # 그대로 보존돼 REQUEST_CHANGES 를 정상 발동한다. 이전에 risk marker 부재
        # 만으로 강등하던 로직이 false positive 를 만들었던 것을 회귀 테스트로 고정.
        pr_file = review_service.PullRequestFile(
            filename="api/handler.py",
            status="modified",
            patch="@@ -10,1 +10,1 @@\n-    return data\n+    return data.value\n",
            additions=1,
            deletions=1,
            right_side_lines={10},
        )
        validated = review_service.validate_mlx_output(
            {
                "summary": "응답 처리 변경.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "api/handler.py",
                        "line": 10,
                        "severity": "Major",
                        "confidence": 0.9,
                        "body": _finding_body(
                            problem="None 반환 시 AttributeError 가 발생합니다.",
                            why="요청이 500 으로 끝납니다.",
                            fix="None 분기를 먼저 처리하세요.",
                        ),
                    }
                ],
            },
            [pr_file],
        )
        self.assertEqual(len(validated.comments), 1)
        self.assertEqual(validated.comments[0].severity, review_service.SEVERITY_MAJOR)
        self.assertEqual(validated.event, "REQUEST_CHANGES")


class SplitLegacyConcernsTests(unittest.TestCase):
    def test_items_with_risk_markers_go_to_must_fix(self) -> None:
        must_fix, suggestions = review_service.split_legacy_concerns(
            [
                "signature 검증 우회 위험이 있습니다.",
                "회귀 테스트가 누락되었습니다.",
                "SQL 인젝션에 취약한 경로가 남아 있습니다.",
            ]
        )
        self.assertEqual(len(must_fix), 3)
        self.assertEqual(suggestions, [])

    def test_items_without_risk_markers_go_to_suggestions(self) -> None:
        must_fix, suggestions = review_service.split_legacy_concerns(
            [
                "네이밍을 payload_meta 로 통일하면 일관성이 좋아집니다.",
                "로그 레벨을 info 로 낮추는 것을 고려해볼 만합니다.",
            ]
        )
        self.assertEqual(must_fix, [])
        self.assertEqual(len(suggestions), 2)

    def test_preserves_input_order_within_each_bucket(self) -> None:
        must_fix, suggestions = review_service.split_legacy_concerns(
            [
                "테스트가 누락되어 있습니다.",
                "네이밍을 정리하면 좋습니다.",
                "보안 우회 위험이 있습니다.",
                "주석 추가를 고려해 볼 수 있습니다.",
            ]
        )
        self.assertEqual(
            must_fix,
            ["테스트가 누락되어 있습니다.", "보안 우회 위험이 있습니다."],
        )
        self.assertEqual(
            suggestions,
            ["네이밍을 정리하면 좋습니다.", "주석 추가를 고려해 볼 수 있습니다."],
        )


class ValidateMlxOutputMustFixRoutingTests(unittest.TestCase):
    def _make_pr_file(self) -> "review_service.PullRequestFile":
        return review_service.PullRequestFile(
            filename="fortune/service.py",
            status="modified",
            patch="@@ -0,0 +1,1 @@\n+x = 1\n",
            additions=1,
            deletions=0,
            right_side_lines={1},
        )

    def test_legacy_concerns_with_risk_marker_are_ignored_without_line_comment(self) -> None:
        # 구 스키마 concerns 는 path/line/confidence 를 담을 수 없으므로 차단성 문구가
        # 있어도 게시하지 않는다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "캐시 로직을 조정했습니다.",
                "event": "COMMENT",
                "positives": [],
                "concerns": ["signature 검증을 건너뛰어 인증 우회 위험이 있습니다."],
                "comments": [],
            },
            [self._make_pr_file()],
        )

        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_direct_must_fix_and_suggestions_fields_are_ignored_without_line_comment(self) -> None:
        # 새 스키마의 top-level buckets 도 line-scoped evidence 가 없으면 게시하지 않는다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "로직을 정리했습니다.",
                "event": "COMMENT",
                "positives": ["캐시 경로를 한곳으로 모아 의도가 드러납니다."],
                "must_fix": ["signature 검증이 비활성화돼 인증 우회 위험이 있습니다."],
                "suggestions": ["네이밍을 payload_meta 로 통일하면 일관성이 좋아집니다."],
                "comments": [],
                # 구 필드는 비어 있어 legacy 경로는 탈 일이 없다.
                "concerns": [],
            },
            [self._make_pr_file()],
        )

        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_legacy_concerns_without_risk_marker_are_ignored(self) -> None:
        # 낮은 위험의 legacy concern 도 라인 근거가 없으면 COMMENT 로 남기지 않는다.
        validated = review_service.validate_mlx_output(
            {
                "summary": "네이밍을 정리했습니다.",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "concerns": ["네이밍 스타일을 payload_meta 로 통일하면 일관성이 좋아집니다."],
                "comments": [],
            },
            [self._make_pr_file()],
        )

        self.assertEqual(validated.must_fix, [])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.event, "APPROVE")

    def test_line_comment_still_drives_request_changes_when_top_level_is_ignored(self) -> None:
        validated = review_service.validate_mlx_output(
            {
                "summary": "응답 처리 변경.",
                "event": "APPROVE",
                "positives": [],
                "must_fix": ["이 문장은 무시됩니다."],
                "suggestions": ["이 문장도 무시됩니다."],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 1,
                        "severity": "Major",
                        "confidence": 0.93,
                        "body": _finding_body(
                            problem="잘못된 상태입니다.",
                            why="요청이 실패합니다.",
                            fix="검증을 추가하세요.",
                        ),
                    }
                ],
            },
            [self._make_pr_file()],
        )

        self.assertEqual(len(validated.comments), 1)
        self.assertEqual(validated.must_fix, ["잘못된 상태입니다."])
        self.assertEqual(validated.suggestions, [])
        self.assertEqual(validated.event, "REQUEST_CHANGES")


class DescriptiveChangeNarrationTests(unittest.TestCase):
    def test_filters_narration_ending_without_risk_marker(self) -> None:
        # 실제 관측된 저신호 concern 패턴. 모두 filter 되어야 한다.
        cases = (
            "nginx-gemini-review.conf 파일의 주석이 한국어로 수정되었습니다.",
            "scripts/local_review_env.example.sh 파일의 주석이 한국어로 수정되었습니다.",
            "새로운 테스트 파일이 추가되었습니다.",
            "diff_right_lines 필드의 타입이 MappingProxyType 으로 변경되었습니다.",
            "헬퍼 함수가 도입되었습니다.",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(
                    review_service.looks_like_descriptive_change_narration(text),
                    msg=f"expected narration filter to match: {text!r}",
                )

    def test_keeps_concern_with_risk_marker_even_if_suffix_matches(self) -> None:
        # 서술형 어미로 끝나면서 동시에 위험 신호가 있는 경우만 화이트리스트 경로가 실제로
        # 실행된다. suffix 가 매칭되지 않는 문자열로는 whitelist 로직을 검증할 수 없어서,
        # 각 케이스는 반드시 DESCRIPTIVE_NARRATION_SUFFIXES 중 하나로 끝나야 한다.
        cases = (
            "인증 우회 위험 코드가 추가되었습니다.",
            "필수 검증이 누락된 채 응답 스키마가 변경되었습니다.",
            "SQL 인젝션에 취약한 쿼리가 도입되었습니다.",
            "중요 에러 처리 로직이 삭제되었습니다.",
        )
        for text in cases:
            with self.subTest(text=text):
                stripped = text.rstrip(review_service.NARRATION_TRAILING_PUNCTUATION)
                self.assertTrue(
                    stripped.endswith(review_service.DESCRIPTIVE_NARRATION_SUFFIXES),
                    msg=(
                        f"test fixture must end with a narration suffix to exercise the "
                        f"risk-marker whitelist branch: {text!r}"
                    ),
                )
                self.assertFalse(
                    review_service.looks_like_descriptive_change_narration(text),
                    msg=f"risk marker should prevent filtering: {text!r}",
                )

    def test_keeps_problem_statement_concerns(self) -> None:
        # '~되었습니다' 어미가 아니라 문제 진술형 concern 은 항상 유지.
        cases = (
            "SQL 인젝션 공격에 취약합니다.",
            "인증 토큰이 로그로 유출될 수 있습니다.",
            "회귀 테스트가 필요합니다.",
            "",
            "   ",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(
                    review_service.looks_like_descriptive_change_narration(text),
                    msg=f"filter should not have matched: {text!r}",
                )

    def test_sanitize_text_items_drops_narration_concerns(self) -> None:
        items = [
            "nginx-gemini-review.conf 파일의 주석이 한국어로 수정되었습니다.",
            "signature 검증이 제거되었습니다. 인증 우회 위험이 있습니다.",
            "새로운 테스트 파일이 추가되었습니다.",
        ]
        sanitized = review_service.sanitize_text_items(items)
        # 서술형 2건은 drop, 위험 신호가 있는 concern 1건만 남아야 한다.
        self.assertEqual(
            sanitized,
            ["signature 검증이 제거되었습니다. 인증 우회 위험이 있습니다."],
        )

    def test_looks_like_praise_only_comment_drops_narration_line_comment(self) -> None:
        # 라인 코멘트 경로에도 서술형 문장이 걸러지는지 확인해 추후 리팩토링 시 연결이
        # 조용히 끊기지 않도록 고정한다.
        self.assertTrue(
            review_service.looks_like_praise_only_comment("주석이 한국어로 수정되었습니다.")
        )
        self.assertTrue(
            review_service.looks_like_praise_only_comment("새로운 테스트 파일이 추가되었습니다.")
        )

    def test_strips_trailing_non_period_punctuation(self) -> None:
        # MLX 가 간혹 마침표 대신 !/?/~ 를 붙여도 동일하게 필터되어야 한다.
        for text in (
            "주석이 수정되었습니다!",
            "필드가 변경되었습니다?",
            "테스트 파일이 추가되었습니다~",
        ):
            with self.subTest(text=text):
                self.assertTrue(
                    review_service.looks_like_descriptive_change_narration(text),
                    msg=f"trailing punctuation should not disable the filter: {text!r}",
                )


class ExtractModelNameFromResultTests(unittest.TestCase):
    def test_returns_model_name_when_meta_is_dict_with_string_value(self) -> None:
        result = {"_meta": {"model_name": "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit"}}
        self.assertEqual(
            review_service.extract_model_name_from_result(result),
            "mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        )

    def test_returns_none_when_meta_is_missing(self) -> None:
        self.assertIsNone(review_service.extract_model_name_from_result({}))

    def test_returns_none_when_meta_is_not_dict(self) -> None:
        # 커스텀 클라이언트가 _meta 에 문자열/리스트를 실어 보내도 AttributeError 없이 None 을 돌려줘야 한다.
        for bad_meta in ("some string", ["list"], 42, True):
            with self.subTest(bad_meta=bad_meta):
                self.assertIsNone(
                    review_service.extract_model_name_from_result({"_meta": bad_meta})
                )

    def test_returns_none_when_model_name_value_is_non_string(self) -> None:
        self.assertIsNone(
            review_service.extract_model_name_from_result({"_meta": {"model_name": None}})
        )
        self.assertIsNone(
            review_service.extract_model_name_from_result({"_meta": {"model_name": 123}})
        )


class BuildReviewPayloadTests(unittest.TestCase):
    def test_body_appends_model_name_footer_when_provided(self) -> None:
        payload = review_service.build_review_payload(
            summary="요약",
            event="COMMENT",
            comments=[],
            positives=["좋은 점"],
            must_fix=[],
            suggestions=[],
            model_name="mlx-community/Qwen2.5-Coder-7B-Instruct-4bit",
        )

        body = payload["body"]
        self.assertIn("---", body)
        self.assertIn(
            "<sub>사용된 모델: mlx-community/Qwen2.5-Coder-7B-Instruct-4bit</sub>",
            body,
        )
        # 푸터는 본문 가장 마지막에 위치해야 추적용 메타 정보가 리뷰 하단에 노출된다.
        self.assertTrue(
            body.rstrip().endswith(
                "<sub>사용된 모델: mlx-community/Qwen2.5-Coder-7B-Instruct-4bit</sub>"
            )
        )

    def test_body_omits_model_name_footer_when_absent(self) -> None:
        payload = review_service.build_review_payload(
            summary="요약",
            event="COMMENT",
            comments=[],
            positives=["좋은 점"],
            must_fix=[],
            suggestions=[],
        )

        self.assertNotIn("사용된 모델", payload["body"])

    def test_body_omits_model_name_footer_for_blank_value(self) -> None:
        # mock 리뷰 클라이언트 등에서 _meta 에 빈 문자열이 들어와도 푸터 라인은 추가되면 안 된다.
        payload = review_service.build_review_payload(
            summary="요약",
            event="COMMENT",
            comments=[],
            positives=["좋은 점"],
            must_fix=[],
            suggestions=[],
            model_name="   ",
        )

        self.assertNotIn("사용된 모델", payload["body"])

    def test_body_renders_must_fix_before_suggestions_before_positives(self) -> None:
        payload = review_service.build_review_payload(
            summary="요약",
            event="REQUEST_CHANGES",
            comments=[],
            positives=["캐시 계약을 명확히 한 부분이 잘 드러납니다."],
            must_fix=["signature 검증이 제거되어 인증 우회가 가능합니다."],
            suggestions=["반복문 내부 DB 조회를 배치로 묶어두면 지연이 줄어듭니다."],
        )

        body = payload["body"]
        # 섹션 순서: must_fix -> suggestions -> positives. 훑을 때 차단성 항목이 먼저 보이도록.
        must_fix_pos = body.index("### 반드시 수정할 사항")
        suggestions_pos = body.index("### 권장 개선사항")
        positives_pos = body.index("### 개선된 점")
        self.assertLess(must_fix_pos, suggestions_pos)
        self.assertLess(suggestions_pos, positives_pos)
        self.assertIn("signature 검증이 제거되어 인증 우회가 가능합니다.", body)
        self.assertIn("반복문 내부 DB 조회를 배치로 묶어두면 지연이 줄어듭니다.", body)

    def test_body_omits_must_fix_section_when_list_is_empty(self) -> None:
        payload = review_service.build_review_payload(
            summary="요약",
            event="COMMENT",
            comments=[],
            positives=["캐시 계약을 명확히 한 부분이 잘 드러납니다."],
            must_fix=[],
            suggestions=["반복문 내부 DB 조회를 배치로 묶어두면 지연이 줄어듭니다."],
        )

        body = payload["body"]
        self.assertNotIn("### 반드시 수정할 사항", body)
        self.assertIn("### 권장 개선사항", body)
        self.assertIn("### 개선된 점", body)


if __name__ == "__main__":
    unittest.main()
