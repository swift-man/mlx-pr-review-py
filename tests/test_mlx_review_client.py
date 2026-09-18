import os
import sys
import types
import unittest
from unittest import mock

from review_runner import mlx_review_client, mlx_review_prompt, review_service


class MlxReviewClientDefaultsTests(unittest.TestCase):
    def test_default_max_tokens_is_bounded_for_webhook_runtime(self) -> None:
        self.assertEqual(mlx_review_client.DEFAULT_MAX_TOKENS, 1600)

    def test_empty_numeric_env_values_fall_back_to_defaults(self) -> None:
        with mock.patch.dict(os.environ, {"MLX_MAX_TOKENS": "", "MLX_TEMPERATURE": "  "}, clear=False):
            self.assertEqual(mlx_review_client.get_env_int("MLX_MAX_TOKENS", 1600), 1600)
            self.assertEqual(mlx_review_client.get_env_float("MLX_TEMPERATURE", 0.0), 0.0)


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

    def test_build_messages_holds_the_runtime_contract(self) -> None:
        """프롬프트가 런타임과 맺은 계약을 고정한다.

        문구가 아니라 **계약**만 검증한다. 이전 버전은 프롬프트 문장을 리터럴로 고정해
        표현을 다듬을 때마다 깨졌고, 정작 계약이 깨졌는지는 알려주지 못했다. 여기서
        고정할 것은 런타임이 실제로 의존하는 것들이다: 스키마 키, comments[] 필드,
        body 형식, severity enum, 등급별 confidence 문턱, 한국어 강제.
        """
        messages = mlx_review_client.build_messages({"repository": "demo/repo", "pull_request": 1, "files": []})
        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]

        # 스키마 키 계약 — validate_mlx_output 가 이 키들을 읽는다.
        self.assertIn("summary, event, positives, must_fix, suggestions, comments", system_prompt)
        self.assertIn("{path, line, severity, confidence, body}", system_prompt)
        # must_fix / suggestions 는 런타임이 무시하므로 빈 배열이어야 한다.
        self.assertIn("must_fix and suggestions must always be empty arrays", system_prompt)

        # body 형식 — extract_confidence_label / has_required_finding_sections 가 파싱한다.
        self.assertIn(
            "Problem: ... Why it matters: ... Suggested fix: ... Confidence: High|Medium|Low",
            system_prompt,
        )

        # severity enum 4단계 — normalize_severity 와 BLOCKING_SEVERITIES 가 의존한다.
        for severity in ("Blocking", "Major", "Minor", "Suggestion"):
            self.assertIn(severity, system_prompt)

        # 등급별 confidence 문턱이 런타임 상수와 일치해야 한다. 어긋나면 모델이
        # 내보낸 지적을 런타임이 조용히 버린다.
        self.assertIn(f"confidence >= {mlx_review_prompt.MIN_BLOCKING_CONFIDENCE:.1f}", system_prompt)
        self.assertIn(f"confidence >= {mlx_review_prompt.MIN_COMMENT_CONFIDENCE:.1f}", system_prompt)
        self.assertEqual(
            mlx_review_prompt.MIN_COMMENT_CONFIDENCE,
            review_service.MIN_MODEL_COMMENT_CONFIDENCE,
        )
        self.assertEqual(
            mlx_review_prompt.MIN_BLOCKING_CONFIDENCE,
            review_service.MIN_BLOCKING_MODEL_COMMENT_CONFIDENCE,
        )

        # 출력 형식 계약 — 파서가 strict JSON 을 기대한다.
        self.assertIn("exactly one JSON object", system_prompt)
        self.assertIn("Never wrap it in markdown fences", system_prompt)
        self.assertIn('"must_fix":[],"suggestions":[]', system_prompt)
        self.assertIn('"event":"APPROVE","positives":[]', system_prompt)

        # 한국어 강제 — 리뷰 대상 독자가 한국어 사용자다.
        self.assertIn("Write every natural-language string in Korean", system_prompt)

        # line anchor 계약 — valid_comment_lines 밖의 라인은 GitHub 이 거부한다.
        self.assertIn("valid_comment_lines", system_prompt)

        # 보안 스윕이 남아 있는지 (모델 성능과 무관하게 유지할 체크리스트)
        self.assertIn("auth or signature checks", system_prompt)

        # 증거 기준의 관문(a~d)과 캡(e)이 구분돼 있는지. 한데 뭉뚱그리면 모델이
        # 외부 의미론 근거(e) 위반 시 지적 자체를 버릴 수 있다.
        self.assertIn("(a) through (d) are gates", system_prompt)
        self.assertIn("(e) is a cap", system_prompt)
        self.assertNotIn("all four", system_prompt)

        # 지적 개수 제한이 comments[] 단일 출구를 가리켜야 한다. 구 스키마의
        # "across must_fix, suggestions, and comments combined" 가 남으면 모델이
        # 개수를 채우려고 빈 배열 규칙을 흔든다.
        self.assertIn("findings in comments[]", system_prompt)
        self.assertNotIn("across must_fix, suggestions, and comments combined", system_prompt)

        # 유저 프롬프트는 짧게 유지한다.
        self.assertIn("위 시스템 지시를 엄격히 따라", user_prompt)
        self.assertIn("JSON 객체 하나만", user_prompt)

        # body 의 Confidence 라벨과 comments[].confidence 숫자를 분리해 지시해야 한다.
        # 한 문장에 묶으면 모델이 body 에 'Confidence: High (0.92)' 를 써서
        # extract_confidence_label(^(high|medium|low)$) 매칭이 실패하고 코멘트가 버려진다.
        self.assertIn("숫자를 덧붙이지 마세요", user_prompt)
        self.assertIn("comments[] 객체의 confidence 필드", user_prompt)

    def test_system_prompt_stays_lean(self) -> None:
        """프롬프트 비대화를 막는다.

        규칙 하나를 덧붙이는 비용은 눈에 안 보이지만 리뷰마다 prefill 로 지불된다.
        실측 960 chars/s 기준 10,480자 프롬프트는 규칙만으로 약 11초였다. 관찰된
        실패마다 금지 규칙을 덧대는 방식으로 되돌아가면 이 테스트가 먼저 깨진다.
        """
        system_prompt = mlx_review_prompt.build_system_prompt()
        self.assertLess(
            len(system_prompt),
            8000,
            "시스템 프롬프트가 8000자를 넘었습니다. 금지 규칙을 덧대는 대신 "
            "'증거 기준' 으로 접을 수 있는지 먼저 검토하세요.",
        )

    def test_prompt_frames_false_positives_and_misses_symmetrically(self) -> None:
        """침묵을 안전한 선택으로 만들지 않는다.

        이전 프롬프트는 'false positives are worse than missed suggestions' 로 한쪽에만
        비용을 매겨, 모델이 확신 없는 정당한 지적까지 버리도록 유도했다.
        """
        system_prompt = mlx_review_prompt.build_system_prompt()
        self.assertIn("equally bad", system_prompt)
        self.assertIn("Do not treat silence as the safe answer", system_prompt)
        self.assertNotIn("False positives are worse", system_prompt)


if __name__ == "__main__":
    unittest.main()
