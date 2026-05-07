"""Unit tests for the HTTP-based remote MLX review client.

mlx_lm 을 import 안 하므로 모델 로드 없이 빠르게 검증할 수 있다. 실 endpoint 가
필요한 부분은 ``urllib.request.urlopen`` 을 mock 으로 대체.
"""
from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest import mock

from review_runner import mlx_remote_review_client as client


def _make_response(payload: dict, status: int = 200) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    return response


class GenerateUrlTests(unittest.TestCase):
    def test_default_loopback(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("MLX_GENERATE_URL", None)
            self.assertEqual(client._generate_url(), client.DEFAULT_GENERATE_URL)

    def test_https_allowed(self) -> None:
        with mock.patch.dict("os.environ", {"MLX_GENERATE_URL": "https://gpu.internal:8002/v1/generate"}):
            self.assertEqual(
                client._generate_url(), "https://gpu.internal:8002/v1/generate"
            )

    def test_file_scheme_rejected(self) -> None:
        # 가장 위험한 케이스 — file:// 가 통과하면 urllib 가 로컬 파일을 읽어 위장
        # 응답을 받게 된다.
        with mock.patch.dict("os.environ", {"MLX_GENERATE_URL": "file:///etc/passwd"}):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("http or https", str(ctx.exception))

    def test_other_schemes_rejected(self) -> None:
        for url in ("ftp://x/y", "data:text/plain,hello", "javascript:1"):
            with mock.patch.dict("os.environ", {"MLX_GENERATE_URL": url}):
                with self.assertRaises(RuntimeError):
                    client._generate_url()

    def test_missing_host_rejected(self) -> None:
        with mock.patch.dict("os.environ", {"MLX_GENERATE_URL": "http:///v1/generate"}):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("host", str(ctx.exception))


class AuthHeaderTests(unittest.TestCase):
    def test_no_token_means_no_header(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=False) as env:
            env.pop("MLX_GENERATE_AUTH_TOKEN", None)
            request = client._build_request("http://x/y", b"{}")
            self.assertNotIn("Authorization", dict(request.header_items()))

    def test_token_sets_bearer_header(self) -> None:
        with mock.patch.dict("os.environ", {"MLX_GENERATE_AUTH_TOKEN": "secret-123"}):
            request = client._build_request("http://x/y", b"{}")
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer secret-123")

    def test_token_strips_whitespace(self) -> None:
        with mock.patch.dict("os.environ", {"MLX_GENERATE_AUTH_TOKEN": "  tok  "}):
            request = client._build_request("http://x/y", b"{}")
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer tok")


class PostGenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [{"role": "user", "content": "hi"}]

    def test_success_returns_parsed_payload(self) -> None:
        response = _make_response({
            "ok": True,
            "text": "안녕",
            "model": "mock",
            "elapsed_ms": 12,
        })
        with mock.patch("urllib.request.urlopen", return_value=response):
            result = client._post_generate(self.messages)
        self.assertEqual(result["text"], "안녕")
        self.assertEqual(result["model"], "mock")

    def test_http_error_raises_runtime_error_with_body(self) -> None:
        body = b'{"ok":false,"error":"unauthorized"}'
        http_exc = urllib.error.HTTPError(
            url="http://x/y", code=401, msg="Unauthorized", hdrs=None, fp=io.BytesIO(body),
        )
        with mock.patch("urllib.request.urlopen", side_effect=http_exc):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertIn("unauthorized", str(ctx.exception))

    def test_http_error_does_not_retry(self) -> None:
        # 4xx/5xx 는 재시도해도 같은 결과 — webhook 처리시간 낭비 방지.
        body = b''
        http_exc = urllib.error.HTTPError(
            url="http://x/y", code=500, msg="Internal", hdrs=None, fp=io.BytesIO(body),
        )
        urlopen = mock.MagicMock(side_effect=http_exc)
        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(RuntimeError):
                client._post_generate(self.messages)
        self.assertEqual(urlopen.call_count, 1)

    def test_url_error_retries_once(self) -> None:
        # 첫 시도는 connection refused, 두 번째에 성공 → 결과 반환.
        success = _make_response({"ok": True, "text": "ok", "model": "m"})
        urlopen = mock.MagicMock(side_effect=[
            urllib.error.URLError("Connection refused"),
            success,
        ])
        with mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep") as sleeper:
            result = client._post_generate(self.messages)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(urlopen.call_count, 2)
        sleeper.assert_called_once_with(1.0)

    def test_url_error_twice_raises(self) -> None:
        urlopen = mock.MagicMock(side_effect=[
            urllib.error.URLError("first"),
            urllib.error.URLError("second"),
        ])
        with mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("after retry", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 2)

    def test_non_json_body_rejected(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b"not json"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("non-JSON", str(ctx.exception))

    def test_missing_text_rejected(self) -> None:
        response = _make_response({"ok": True, "model": "x"})  # text 누락
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("unexpected payload", str(ctx.exception))

    def test_ok_false_rejected(self) -> None:
        response = _make_response({"ok": False, "text": "...", "error": "x"})
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                client._post_generate(self.messages)


class ReadErrorBodyTests(unittest.TestCase):
    def test_decodes_payload(self) -> None:
        exc = urllib.error.HTTPError(
            url="x", code=400, msg="bad", hdrs=None, fp=io.BytesIO(b"err"),
        )
        self.assertEqual(client._read_error_body(exc), "err")

    def test_swallows_oserror_with_warning(self) -> None:
        # 본문 read 가 OSError 를 던지는 경우 (예: 끊긴 socket). _read_error_body
        # 가 호출자에게 빈 문자열을 돌려주고 경고만 stderr 에 남겨야 한다.
        broken_fp = io.BytesIO(b"")
        broken_fp.read = mock.MagicMock(side_effect=OSError("broken"))  # type: ignore[method-assign]
        exc = urllib.error.HTTPError(
            url="x", code=500, msg="internal", hdrs=None, fp=broken_fp,
        )
        with mock.patch("sys.stderr") as fake_stderr:
            result = client._read_error_body(exc)
        self.assertEqual(result, "")
        fake_stderr.write.assert_called()  # 로그 한 번이라도 남기는지만 검증


if __name__ == "__main__":
    unittest.main()
