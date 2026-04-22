import json
from pathlib import Path
import unittest

from review_runner import mlx_review_parser


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mlx_outputs"


def read_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


class MlxReviewParserTests(unittest.TestCase):
    def test_parse_and_normalize_tracks_strict_json_drop_reasons(self) -> None:
        normalized, metadata = mlx_review_parser.parse_and_normalize_model_output(read_fixture("strict_json.json"))

        self.assertEqual(metadata["parse_mode"], "strict_json")
        self.assertEqual(metadata["parse_error"], "")
        self.assertEqual(metadata["raw_comment_count"], 6)
        self.assertEqual(metadata["normalized_comment_count"], 1)
        self.assertEqual(
            metadata["dropped_comment_reasons"],
            {
                "duplicate_comment": 1,
                "invalid_body": 1,
                "invalid_line": 1,
                "missing_path": 1,
                "non_object_comment": 1,
            },
        )
        self.assertEqual(
            normalized["comments"],
            [
                {
                    "path": "fortune/cache.py",
                    "line": 27,
                    "body": "캐시 만료 분기와 DB 조회 분기가 함께 바뀌었으니 회귀 테스트를 추가해두는 편이 안전합니다.",
                    "severity": "Major",
                }
            ],
        )

    def test_parse_and_normalize_salvages_markdown_sections(self) -> None:
        normalized, metadata = mlx_review_parser.parse_and_normalize_model_output(read_fixture("salvaged_sections.txt"))

        self.assertEqual(metadata["parse_mode"], "salvaged_output")
        self.assertIn("did not contain a JSON object", metadata["parse_error"])
        self.assertEqual(normalized["summary"], "운세 캐시 구조를 정리했습니다.")
        self.assertEqual(
            normalized["positives"],
            ["dataclass를 도입해 캐시 엔트리 필드 계약이 분명해지고 초기화 보일러플레이트가 줄었습니다."],
        )
        # 레거시 픽스처는 concerns 섹션만 썼으므로 파서는 이를 legacy_concerns 로 노출한다.
        # 서비스 계층이 risk marker 기반으로 must_fix / suggestions 로 나눠 흡수한다.
        self.assertEqual(normalized["must_fix"], [])
        self.assertEqual(normalized["suggestions"], [])
        self.assertEqual(
            normalized["legacy_concerns"],
            ["캐시 만료와 원본 조회 흐름이 함께 바뀌어 경계 조건 테스트를 추가하면 회귀 위험을 줄일 수 있습니다."],
        )
        self.assertEqual(normalized["comments"], [])

    def test_parse_and_normalize_uses_fallback_for_unusable_output(self) -> None:
        normalized, metadata = mlx_review_parser.parse_and_normalize_model_output(read_fixture("fallback_plain_text.txt"))

        self.assertEqual(metadata["parse_mode"], "fallback_response")
        self.assertIn("did not contain a JSON object", metadata["parse_error"])
        self.assertEqual(normalized["summary"], mlx_review_parser.DEFAULT_SUMMARY)
        self.assertEqual(normalized["positives"], mlx_review_parser.DEFAULT_POSITIVES)
        self.assertEqual(normalized["must_fix"], [])
        self.assertEqual(normalized["suggestions"], [])
        self.assertEqual(normalized["legacy_concerns"], [])
        self.assertEqual(normalized["comments"], [])


class MlxReviewParserSeverityTests(unittest.TestCase):
    def test_normalize_comment_preserves_severity_raw_value(self) -> None:
        # 파서는 severity 를 정규화하지 않고 raw 값을 그대로 흘려보낸다.
        # 정규화는 review_service.normalize_severity 에서 처리한다.
        normalized, reason = mlx_review_parser.normalize_comment(
            {"path": "a.py", "line": 1, "body": "본문", "severity": "Critical"}
        )
        self.assertIsNone(reason)
        assert normalized is not None
        self.assertEqual(normalized["severity"], "Critical")

    def test_normalize_comment_returns_none_severity_when_key_missing(self) -> None:
        normalized, reason = mlx_review_parser.normalize_comment(
            {"path": "a.py", "line": 1, "body": "본문"}
        )
        self.assertIsNone(reason)
        assert normalized is not None
        # severity 키가 없어도 None 으로 명시적으로 보존돼야 서비스 계층이 Minor 로 폴백한다.
        self.assertIsNone(normalized["severity"])

    def test_parse_and_normalize_carries_severity_end_to_end(self) -> None:
        # 모델이 보낸 severity 가 파싱 → 정규화 파이프라인 끝까지 살아남는지 확인.
        # 이 경로가 끊기면 모든 모델 라인 코멘트가 서비스 계층에서 Minor 로 강등된다.
        raw_output = json.dumps(
            {
                "summary": "테스트 요약",
                "event": "REQUEST_CHANGES",
                "positives": [],
                "must_fix": [],
                "suggestions": [],
                "comments": [
                    {
                        "path": "fortune/service.py",
                        "line": 10,
                        "severity": "Critical",
                        "body": "서명 검증이 제거되어 인증 우회 위험이 있습니다.",
                    }
                ],
            }
        )
        normalized, _metadata = mlx_review_parser.parse_and_normalize_model_output(raw_output)
        self.assertEqual(len(normalized["comments"]), 1)
        self.assertEqual(normalized["comments"][0]["severity"], "Critical")


if __name__ == "__main__":
    unittest.main()
