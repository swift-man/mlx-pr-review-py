"""Unit tests for the HTTP-based remote MLX review client.

mlx_lm 을 import 안 하므로 모델 로드 없이 빠르게 검증할 수 있다. 실 endpoint 가
필요한 부분은 ``urllib.request.urlopen`` 을 mock 으로 대체.
"""
from __future__ import annotations

import io
import json
import os
import unittest
import urllib.error
from unittest import mock

from review_runner import mlx_remote_review_client as client


_REMOTE_ENV_KEYS = (
    "MLX_GENERATE_URL",
    "MLX_GENERATE_AUTH_TOKEN",
    "MLX_GENERATE_TIMEOUT",
    "MLX_MAX_TOKENS",
    "MLX_TEMPERATURE",
    "MLX_TOP_P",
    "MLX_REPETITION_PENALTY",
    "MLX_REPETITION_CONTEXT_SIZE",
    "MLX_MODEL",
)


def _isolated_env(**overrides: str) -> dict[str, str]:
    """현재 셸의 MLX_* 환경 변수가 테스트 동작에 새는 걸 막기 위해 모든 관련 키를
    명시적으로 비운 뒤 overrides 만 다시 주입 (codex Round 2 권장)."""
    env: dict[str, str] = {}
    for key in _REMOTE_ENV_KEYS:
        env[key] = ""
    env.update(overrides)
    return env


def _make_response(payload: dict, status: int = 200) -> mock.MagicMock:
    response = mock.MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status = status
    return response


class GenerateUrlTests(unittest.TestCase):
    def test_default_loopback(self) -> None:
        with mock.patch.dict(os.environ, _isolated_env(), clear=False):
            self.assertEqual(client._generate_url(), client.DEFAULT_GENERATE_URL)

    def test_https_allowed(self) -> None:
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="https://gpu.internal:8002/v1/generate")):
            self.assertEqual(
                client._generate_url(), "https://gpu.internal:8002/v1/generate"
            )

    def test_file_scheme_rejected(self) -> None:
        # 가장 위험한 케이스 — file:// 가 통과하면 urllib 가 로컬 파일을 읽어 위장
        # 응답을 받게 된다.
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="file:///etc/passwd")):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("http or https", str(ctx.exception))

    def test_other_schemes_rejected(self) -> None:
        for url in ("ftp://x/y", "data:text/plain,hello", "javascript:1"):
            with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL=url)):
                with self.assertRaises(RuntimeError):
                    client._generate_url()

    def test_missing_host_rejected(self) -> None:
        # http:///path 형태 — netloc 자체가 비었음
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="http:///v1/generate")):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("host", str(ctx.exception))

    def test_empty_hostname_rejected(self) -> None:
        # http://:8002/path — netloc 은 ":8002" 라 통과하지만 hostname 이 비어 있음
        # (CodeRabbit Round 2 Minor).
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="http://:8002/v1/generate")):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("host", str(ctx.exception))

    def test_invalid_port_rejected(self) -> None:
        # http://gpu:bad/path — port 가 정수 아님. urlparse 가 .port 접근 시점에
        # ValueError 던지는데 그 누수를 막기 위해 RuntimeError 로 통일 (CodeRabbit
        # Round 3 Minor).
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="http://gpu:bad/v1/generate")):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("port", str(ctx.exception))

    def test_invalid_port_error_sanitizes_url(self) -> None:
        with mock.patch.dict(
            os.environ,
            _isolated_env(MLX_GENERATE_URL="http://user:secret@gpu:bad/v1/generate?token=abc"),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
        msg = str(ctx.exception)
        self.assertIn("http://gpu/v1/generate", msg)
        self.assertNotIn("user:secret", msg)
        self.assertNotIn("token=abc", msg)

    def test_port_zero_rejected(self) -> None:
        # 0 은 유효하지 않은 포트 — urlparse 는 통과시키지만 우리는 거부.
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_URL="http://gpu:0/v1/generate")):
            with self.assertRaises(RuntimeError) as ctx:
                client._generate_url()
            self.assertIn("port", str(ctx.exception))


class SanitizeUrlForLoggingTests(unittest.TestCase):
    """리뷰 metadata 에 노출할 URL 에서 secrets / query 를 제거 (CodeRabbit Round 2 Major)."""

    def test_strips_userinfo(self) -> None:
        result = client._sanitize_url_for_logging("http://user:secret@gpu.local:8002/v1/generate")
        self.assertEqual(result, "http://gpu.local:8002/v1/generate")

    def test_strips_query_and_fragment(self) -> None:
        result = client._sanitize_url_for_logging("http://gpu/v1/generate?token=secret#fragment")
        self.assertEqual(result, "http://gpu/v1/generate")

    def test_keeps_port(self) -> None:
        result = client._sanitize_url_for_logging("https://gpu.internal:9443/v1/generate")
        self.assertEqual(result, "https://gpu.internal:9443/v1/generate")

    def test_returns_empty_for_missing_host(self) -> None:
        self.assertEqual(client._sanitize_url_for_logging("http://"), "")

    def test_invalid_port_does_not_prevent_sanitizing(self) -> None:
        result = client._sanitize_url_for_logging("http://user:secret@gpu:bad/v1/generate?token=x")
        self.assertEqual(result, "http://gpu/v1/generate")


class AuthHeaderTests(unittest.TestCase):
    def test_no_token_means_no_header(self) -> None:
        with mock.patch.dict(os.environ, _isolated_env()):
            request = client._build_request("http://x/y", b"{}")
            self.assertNotIn("Authorization", dict(request.header_items()))

    def test_token_sets_bearer_header(self) -> None:
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_AUTH_TOKEN="secret-123")):
            request = client._build_request("http://x/y", b"{}")
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer secret-123")

    def test_token_strips_whitespace(self) -> None:
        with mock.patch.dict(os.environ, _isolated_env(MLX_GENERATE_AUTH_TOKEN="  tok  ")):
            request = client._build_request("http://x/y", b"{}")
            headers = {k.lower(): v for k, v in request.header_items()}
            self.assertEqual(headers["authorization"], "Bearer tok")


class PostGenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.messages = [{"role": "user", "content": "hi"}]

    def _patch_env(self, **overrides: str):
        return mock.patch.dict(os.environ, _isolated_env(**overrides), clear=False)

    def test_success_returns_parsed_payload(self) -> None:
        response = _make_response({
            "ok": True,
            "text": "안녕",
            "model": "mock",
            "elapsed_ms": 12,
        })
        with self._patch_env(), mock.patch("urllib.request.urlopen", return_value=response):
            result = client._post_generate(self.messages)
        self.assertEqual(result["text"], "안녕")
        self.assertEqual(result["model"], "mock")

    def test_http_4xx_raises_immediately_no_retry(self) -> None:
        body = b'{"ok":false,"error":"bad request"}'
        http_exc = urllib.error.HTTPError(
            url="http://x/y", code=400, msg="Bad Request", hdrs=None, fp=io.BytesIO(body),
        )
        urlopen = mock.MagicMock(side_effect=http_exc)
        with self._patch_env(), mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("HTTP 400", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_http_401_raises_immediately(self) -> None:
        body = b'{"ok":false,"error":"unauthorized"}'
        http_exc = urllib.error.HTTPError(
            url="http://x/y", code=401, msg="Unauthorized", hdrs=None, fp=io.BytesIO(body),
        )
        urlopen = mock.MagicMock(side_effect=http_exc)
        with self._patch_env(), mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1)

    def test_http_500_retries_once(self) -> None:
        # 502/503/500 같은 일시 5xx 는 재시도 (gemini Round 3 Major).
        body = b''
        first = urllib.error.HTTPError(
            url="http://x/y", code=502, msg="Bad Gateway", hdrs=None, fp=io.BytesIO(body),
        )
        success = _make_response({"ok": True, "text": "ok", "model": "m"})
        urlopen = mock.MagicMock(side_effect=[first, success])
        with self._patch_env(), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep") as sleeper:
            result = client._post_generate(self.messages)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(urlopen.call_count, 2)
        sleeper.assert_called_once_with(1.0)

    def test_http_500_twice_raises(self) -> None:
        body = b''
        urlopen = mock.MagicMock(side_effect=[
            urllib.error.HTTPError(url="x", code=503, msg="busy", hdrs=None, fp=io.BytesIO(body)),
            urllib.error.HTTPError(url="x", code=503, msg="busy", hdrs=None, fp=io.BytesIO(body)),
        ])
        with self._patch_env(), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("HTTP 503", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 2)

    def test_url_error_retries_once(self) -> None:
        success = _make_response({"ok": True, "text": "ok", "model": "m"})
        urlopen = mock.MagicMock(side_effect=[
            urllib.error.URLError("Connection refused"),
            success,
        ])
        with self._patch_env(), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep") as sleeper:
            result = client._post_generate(self.messages)
        self.assertEqual(result["text"], "ok")
        self.assertEqual(urlopen.call_count, 2)
        sleeper.assert_called_once_with(1.0)

    def test_url_error_twice_raises_with_sanitized_url(self) -> None:
        # 에러 메시지에는 sanitize 된 URL 만 들어가야 한다 — userinfo/query 가
        # 노출되면 안 됨.
        urlopen = mock.MagicMock(side_effect=[
            urllib.error.URLError("first"),
            urllib.error.URLError("second"),
        ])
        with self._patch_env(MLX_GENERATE_URL="http://user:pw@gpu/v1/generate?token=abc"), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        msg = str(ctx.exception)
        self.assertIn("after retry", msg)
        self.assertNotIn("user:pw", msg)
        self.assertNotIn("token=abc", msg)
        self.assertEqual(urlopen.call_count, 2)

    def test_read_timeout_raises_without_retry_with_sanitized_url(self) -> None:
        response = mock.MagicMock()
        response.read.side_effect = TimeoutError("timed out")
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        urlopen = mock.MagicMock(return_value=response)

        with self._patch_env(MLX_GENERATE_URL="http://user:pw@gpu/v1/generate?token=abc"), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep") as sleeper:
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)

        msg = str(ctx.exception)
        self.assertIn("timed out after", msg)
        self.assertIn("http://gpu/v1/generate", msg)
        self.assertNotIn("user:pw", msg)
        self.assertNotIn("token=abc", msg)
        self.assertEqual(urlopen.call_count, 1)
        sleeper.assert_not_called()

    def test_url_error_wrapping_timeout_raises_without_retry(self) -> None:
        urlopen = mock.MagicMock(side_effect=urllib.error.URLError(TimeoutError("timed out")))

        with self._patch_env(MLX_GENERATE_TIMEOUT="9"), \
             mock.patch("urllib.request.urlopen", urlopen), \
             mock.patch("time.sleep") as sleeper:
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)

        self.assertIn("timed out after 9.0s", str(ctx.exception))
        self.assertEqual(urlopen.call_count, 1)
        sleeper.assert_not_called()

    def test_non_json_body_rejected(self) -> None:
        response = mock.MagicMock()
        response.read.return_value = b"not json"
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with self._patch_env(), mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("non-JSON", str(ctx.exception))

    def test_missing_text_rejected(self) -> None:
        response = _make_response({"ok": True, "model": "x"})  # text 누락
        with self._patch_env(), mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(RuntimeError) as ctx:
                client._post_generate(self.messages)
        self.assertIn("unexpected payload", str(ctx.exception))

    def test_ok_false_rejected(self) -> None:
        response = _make_response({"ok": False, "text": "...", "error": "x"})
        with self._patch_env(), mock.patch("urllib.request.urlopen", return_value=response):
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


class ReviewPayloadMetaTests(unittest.TestCase):
    """review_payload 가 _meta.generate_url 에 sanitize 된 URL 만 넣는지 검증
    (CodeRabbit Round 2 Major)."""

    def test_meta_url_strips_userinfo_and_query(self) -> None:
        response = _make_response({
            "ok": True,
            "text": json.dumps({"summary": "x", "event": "COMMENT", "comments": []}),
            "model": "mock",
            "elapsed_ms": 1,
        })
        env = _isolated_env(
            MLX_GENERATE_URL="http://reviewer:secret@gpu.local:8002/v1/generate?debug=1",
        )
        payload = {
            "repository": "x/y",
            "pull_request": {"number": 1, "title": "t", "body": ""},
            "files": [{"path": "a", "patch": "@@ -1 +1 @@\n hi", "additions": 0, "deletions": 0, "right_lines": []}],
        }
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("urllib.request.urlopen", return_value=response):
            result = client.review_payload(payload)
        meta = result.get("_meta") or {}
        self.assertEqual(meta["generate_url"], "http://gpu.local:8002/v1/generate")
        self.assertNotIn("secret", json.dumps(meta))
        self.assertNotIn("debug", json.dumps(meta))


if __name__ == "__main__":
    unittest.main()
