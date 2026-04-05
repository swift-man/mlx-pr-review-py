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
        self.assertIn("When writing positives, explain the technical reason", system_prompt)
        self.assertIn("Do not place praise, strengths, or neutral observations inside concerns.", system_prompt)
        self.assertIn("Do not ask to rename internal variable names", system_prompt)
        self.assertIn("positives 에는 왜 좋은지와 어떤 기술적 효과가 있는지까지 설명하세요.", user_prompt)
        self.assertIn("concerns 에는 실제 문제, 위험, 누락된 검증이나 테스트만 적고", user_prompt)
        self.assertIn("내부 변수명, 상수명, 클래스명, 함수명을 영어에서 한국어로 바꾸라고 요구하지 마세요.", user_prompt)


if __name__ == "__main__":
    unittest.main()
