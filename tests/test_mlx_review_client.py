import os
import sys
import types
import unittest
from unittest import mock

from review_runner import mlx_review_client


class MlxReviewClientDeviceTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("MLX_DEVICE", None)

    def test_configure_default_device_uses_cpu_when_requested(self) -> None:
        fake_core = types.ModuleType("mlx.core")
        fake_core.cpu = object()
        fake_core.gpu = object()
        fake_core.set_default_device = mock.Mock()
        fake_mlx = types.ModuleType("mlx")
        fake_mlx.core = fake_core

        with mock.patch.dict(os.environ, {"MLX_DEVICE": "cpu"}, clear=False):
            with mock.patch.dict(sys.modules, {"mlx": fake_mlx, "mlx.core": fake_core}, clear=False):
                device_name = mlx_review_client.configure_default_device()

        self.assertEqual(device_name, "cpu")
        fake_core.set_default_device.assert_called_once_with(fake_core.cpu)

    def test_load_runtime_applies_requested_device_before_loading_model(self) -> None:
        fake_core = types.ModuleType("mlx.core")
        fake_core.cpu = object()
        fake_core.gpu = object()
        fake_core.set_default_device = mock.Mock()
        fake_mlx = types.ModuleType("mlx")
        fake_mlx.core = fake_core

        fake_mlx_lm = types.ModuleType("mlx_lm")

        def fake_load(*args, **kwargs):
            self.assertEqual(fake_core.set_default_device.call_count, 1)
            return ("model", "tokenizer")

        fake_mlx_lm.load = fake_load

        with mock.patch.dict(os.environ, {"MLX_DEVICE": "cpu"}, clear=False):
            with mock.patch.dict(
                sys.modules,
                {"mlx": fake_mlx, "mlx.core": fake_core, "mlx_lm": fake_mlx_lm},
                clear=False,
            ):
                with mock.patch.object(mlx_review_client, "_MODEL", None):
                    with mock.patch.object(mlx_review_client, "_TOKENIZER", None):
                        model, tokenizer = mlx_review_client.load_runtime()

        self.assertEqual((model, tokenizer), ("model", "tokenizer"))
        fake_core.set_default_device.assert_called_once_with(fake_core.cpu)

    def test_configure_default_device_rejects_unknown_value(self) -> None:
        with mock.patch.dict(os.environ, {"MLX_DEVICE": "neural-engine"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "MLX_DEVICE must be one of: auto, cpu, gpu"):
                mlx_review_client.configure_default_device()

    def test_build_messages_uses_strict_reviewer_persona_and_new_schema(self) -> None:
        """Phase 2 프롬프트 재작성 결과를 고정한다.

        시스템 프롬프트는 역할 프라이밍 + 우선순위 리스트 + 새 스키마 계약(must_fix/
        suggestions/positives 삼분할) 을 담고 있어야 한다. 유저 프롬프트는 규칙 나열
        대신 짧은 지시만 유지한다.
        """
        messages = mlx_review_client.build_messages({"repository": "demo/repo", "pull_request": 1, "files": []})

        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]

        # 역할 프라이밍
        self.assertIn("senior software engineer acting as a strict, evidence-driven", system_prompt)

        # 우선순위 리스트 (1~6번이 명시되는지)
        self.assertIn("Review priority", system_prompt)
        self.assertIn("Bugs, missing exception handling", system_prompt)
        self.assertIn("Concurrency, thread-safety", system_prompt)
        self.assertIn("Security", system_prompt)

        # 새 스키마 키 계약
        self.assertIn("summary, event, positives, must_fix, suggestions, comments", system_prompt)
        self.assertIn("positives, must_fix, suggestions must be JSON arrays of strings", system_prompt)

        # 핵심 원칙
        self.assertIn("Never speculate", system_prompt)
        self.assertIn("Reject vague phrasing", system_prompt)
        self.assertIn("'~가 추가되었습니다'", system_prompt)
        self.assertIn("Prefer empty arrays over padding", system_prompt)

        # 필드 정의가 세 바구니 모두 명시돼 있는지
        self.assertIn("must_fix: items that must be addressed before merge", system_prompt)
        self.assertIn("suggestions: nice-to-have improvements", system_prompt)
        self.assertIn("positives: things THIS PR actually improves", system_prompt)

        # event 규칙이 runtime 강제 안내를 포함
        self.assertIn("runtime rewrites event based on must_fix", system_prompt)

        # 보안/계약 체크리스트 보존
        self.assertIn("disable validation", system_prompt)
        self.assertIn("bypass auth/signature", system_prompt)
        self.assertIn("log a token/secret", system_prompt)

        # 스키마 예시와 빈 결과 예시가 새 키로 갱신됐는지
        self.assertIn('"must_fix":["한국어 반드시 수정할 사항"]', system_prompt)
        self.assertIn('"suggestions":["한국어 권장 개선사항"]', system_prompt)
        self.assertIn('"must_fix":[],"suggestions":[]', system_prompt)

        # 라인 코멘트 severity 4단계 정의가 프롬프트에 노출되는지
        self.assertIn("Severity levels for comments", system_prompt)
        self.assertIn("Critical", system_prompt)
        self.assertIn("Major", system_prompt)
        self.assertIn("Minor", system_prompt)
        self.assertIn("Suggestion", system_prompt)
        self.assertIn('"severity":"Major"', system_prompt)
        # event 강제 규칙이 must_fix 와 라인 코멘트 두 경로를 모두 명시적으로 포함하는지.
        self.assertIn(
            "REQUEST_CHANGES is triggered by any must_fix item or by any Critical/Major line comment",
            system_prompt,
        )
        self.assertIn("Minor and Suggestion line comments alone keep event at COMMENT", system_prompt)

        # 유저 프롬프트는 짧게 유지하면서 한국어 강제와 빈 결과 허용만 분명히 전달
        self.assertIn("위 시스템 지시를 엄격히 따라", user_prompt)
        self.assertIn("JSON 객체 하나만", user_prompt)
        self.assertIn("must_fix, suggestions, comments 가 비어 있어도 괜찮습니다", user_prompt)
        self.assertIn("diff 가 이미 수행한 변경을 사실 서술로 옮기지 마세요", user_prompt)


if __name__ == "__main__":
    unittest.main()
