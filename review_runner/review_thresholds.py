"""리뷰 confidence 문턱의 단일 진실 공급원.

프롬프트(mlx_review_prompt)와 런타임 검증(review_service)이 같은 값을 써야 한다.
어긋나면 모델이 규칙대로 내보낸 지적을 런타임이 조용히 버리는데, 로그상으로는
'모델이 아무것도 안 냈다' 와 구분되지 않아 원인을 찾기 어렵다.

두 모듈에 각각 상수를 두고 테스트로 일치를 검증하던 구조였으나, 그건 사후 검증이다.
여기서 한 번만 정의해 양쪽이 import 하도록 바꿨다.
"""

from __future__ import annotations

# 머지를 막는 등급(Blocking/Major). 오탐 비용이 커서 높게 유지한다.
MIN_BLOCKING_CONFIDENCE = 0.8

# 머지를 막지 않는 등급(Minor/Suggestion). 0.8 을 그대로 쓰면 확신 0.6~0.8 구간의
# 정당한 지적이 전부 버려져 리뷰가 "치명적 버그 아니면 침묵" 이 된다.
MIN_COMMENT_CONFIDENCE = 0.6

# top-level finding(must_fix/suggestions) 복구 경로. 모델이 numeric confidence 를
# 주지 않아 본문 라벨에서 역산하므로 comments[] 보다 증거가 약하다. 코멘트 문턱을
# 공유하면 0.6 으로 낮출 때 medium 라벨(0.7)까지 조용히 통과한다.
MIN_TOP_LEVEL_FINDING_CONFIDENCE = 0.8
