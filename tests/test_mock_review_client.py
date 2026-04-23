"""Smoke tests for the deterministic mock review client.

mock_review_client 는 실제 MLX 추론 없이 GitHub Review API 연동만 검증할 때 사용된다.
Phase 2 스키마 (must_fix / suggestions / comments[].severity) 를 따르는지 고정해,
구 스키마 호환 경로(legacy_concerns) 에 조용히 의존하지 않도록 방어선을 둔다.
"""

import sys
import types
import unittest

# review_service 는 certifi / jwt 에 의존하므로 테스트 환경에서 가벼운 stub 으로 대체한다.
# 여기서는 severity 상수만 필요해서 import 할 뿐, 네트워크/암호 모듈 전체가 로드될 필요는 없다.
if "certifi" not in sys.modules:
    fake_certifi = types.ModuleType("certifi")
    fake_certifi.where = lambda: "/tmp/fake-cert.pem"
    sys.modules["certifi"] = fake_certifi

if "jwt" not in sys.modules:
    sys.modules["jwt"] = types.ModuleType("jwt")

from review_runner import mock_review_client, review_service


class MockReviewClientSchemaTests(unittest.TestCase):
    def _sample_payload(self) -> dict:
        return {
            "repository": "owner/repo",
            "pull_request": 1,
            "files": [
                {
                    "path": "src/handler.py",
                    "valid_comment_lines": [12, 13, 14],
                }
            ],
        }

    def test_build_response_uses_phase2_schema_keys(self) -> None:
        response = mock_review_client.build_response(self._sample_payload())

        # 구 스키마 키(concerns) 는 더 이상 생성되면 안 된다. 남아 있으면 legacy_concerns
        # 호환 경로만 타게 돼 실제 새 스키마 경로 테스트가 되지 않는다.
        self.assertNotIn("concerns", response)

        # Phase 2 필드가 전부 포함되는지
        for key in ("summary", "event", "positives", "must_fix", "suggestions", "comments"):
            with self.subTest(key=key):
                self.assertIn(key, response, msg=f"missing top-level key: {key}")

    def test_line_comment_includes_severity(self) -> None:
        response = mock_review_client.build_response(self._sample_payload())
        self.assertEqual(len(response["comments"]), 1)
        comment = response["comments"][0]
        # severity 가 있어야 build_review_payload 에서 '[Minor]' 같은 접두사로 렌더된다.
        self.assertIn("severity", comment)
        # mock 은 반복 테스트 시 PR 이 REQUEST_CHANGES 로 쌓이지 않도록 non-blocking 등급을 쓴다.
        self.assertEqual(comment["severity"], "Minor")
        self.assertEqual(comment["path"], "src/handler.py")
        self.assertEqual(comment["line"], 12)

    def test_event_stays_comment_so_repeat_runs_do_not_block_pr(self) -> None:
        response = mock_review_client.build_response(self._sample_payload())
        # must_fix 가 비어 있으므로 event 는 COMMENT 로 남아야 반복 E2E 테스트 시
        # 같은 PR 이 차단 상태로 쌓이지 않는다.
        self.assertEqual(response["must_fix"], [])
        self.assertEqual(response["event"], "COMMENT")

    def test_raises_when_no_valid_comment_target_found(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "No valid comment target"):
            mock_review_client.build_response({"files": []})

    def test_severity_string_matches_review_service_constant(self) -> None:
        """mock 이 emit 하는 severity 문자열이 review_service 의 정규화 매핑 키와
        정확히 일치하는지 고정한다. 상수 값이 바뀌면 mock 출력이 매핑에서 탈락해
        Minor 폴백으로 내려가므로, 테스트는 통과하면서 실제 E2E 는 severity 검증을
        못 하는 silent drift 가 생길 수 있다. 둘을 같은 소스에 묶어두는 회귀 방어선.
        """
        response = mock_review_client.build_response(self._sample_payload())
        self.assertEqual(
            response["comments"][0]["severity"],
            review_service.SEVERITY_MINOR,
        )


if __name__ == "__main__":
    unittest.main()
