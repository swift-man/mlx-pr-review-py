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

    def test_build_messages_requests_technical_positives_and_empty_concerns_when_no_issue(self) -> None:
        messages = mlx_review_client.build_messages({"repository": "demo/repo", "pull_request": 1, "files": []})

        system_prompt = messages[0]["content"]
        user_prompt = messages[1]["content"]
        self.assertIn("The summary should explain the problem or maintenance burden being addressed", system_prompt)
        self.assertIn("Good summaries follow this pattern: problem or motivation -> change -> expected effect", system_prompt)
        self.assertIn("When writing positives, explain the technical reason", system_prompt)
        self.assertIn("Do not place praise, strengths, or neutral observations inside concerns.", system_prompt)
        self.assertIn("Never restate, paraphrase, or rephrase text that the diff itself introduces", system_prompt)
        self.assertIn("Prefer returning an empty concerns array over padding it with restated diff content.", system_prompt)
        self.assertIn("Do not turn repository process rules into code review findings.", system_prompt)
        self.assertIn("Do not ask to rename internal variable names", system_prompt)
        self.assertIn("Good positives follow this pattern: changed construct -> technical role -> concrete effect", system_prompt)
        self.assertIn("If the diff adds test scaffolding such as dummy classes, fake modules, monkeypatching, or sys.modules registration", system_prompt)
        self.assertIn("summary 는 무엇을 추가했는지만 나열하지 말고", user_prompt)
        self.assertIn("기존 문제나 불편 -> 이번 변경 -> 기대 효과", user_prompt)
        self.assertIn("흩어진 운세 데이터 표현과 중복 조회 부담을 줄이기 위해", user_prompt)
        self.assertIn("positives 에는 왜 좋은지와 어떤 기술적 효과가 있는지까지 설명하세요.", user_prompt)
        self.assertIn("PR 제목/description 언어, 커밋 메시지 스타일, AGENTS.md 작업 규칙 자체를 코드 리뷰 concern 으로 적지 마세요.", user_prompt)
        self.assertIn("그 요소가 코드에서 하는 역할", user_prompt)
        self.assertIn("__init__, __repr__, __eq__", user_prompt)
        self.assertIn("types.ModuleType", user_prompt)
        self.assertIn("sys.modules", user_prompt)
        self.assertIn("concerns 에는 실제 문제, 위험, 누락된 검증이나 테스트만 적고", user_prompt)
        self.assertIn("diff 의 + 라인에서 새로 추가된 주석, docstring, TODO 문구", user_prompt)
        self.assertIn("# 환율은 전용 스케줄러가 갱신한다", user_prompt)
        self.assertIn("concerns 가 비어 있어도 됩니다", user_prompt)
        self.assertIn("내부 변수명, 상수명, 클래스명, 함수명을 영어에서 한국어로 바꾸라고 요구하지 마세요.", user_prompt)


if __name__ == "__main__":
    unittest.main()
