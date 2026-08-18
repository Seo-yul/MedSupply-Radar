"""위험등급 ↔ CSS 클래스 매핑 단일 모듈.

두 축을 혼동하지 않는다:
- 위험등급(이 모듈의 대상): 위험 > 경고 > 주의 > 정상 — 심각한 순.
- 공급상태 라벨(불변, 이 모듈과 무관): 현재 품절 / 품절 예상 / 공급중단 / 정상화.
- 알림 심각도(불변, 이 모듈과 무관): 긴급 / 높음 / 확인.

뷰에서 등급별 CSS 클래스가 필요하면 항상 이 모듈의 grade_css()를 거친다
(situation.py 등에 하드코딩된 매핑 dict를 두지 않는다).
"""

from __future__ import annotations

GRADE_ORDER = ["위험", "경고", "주의", "정상"]  # 심각한 순

_GRADE_CSS = {"위험": "critical", "경고": "high", "주의": "watch", "정상": "safe"}


def grade_css(grade: str) -> str:
    """등급 문자열 → CSS 클래스. 미지 등급은 ValueError."""
    try:
        return _GRADE_CSS[grade]
    except KeyError as exc:
        raise ValueError(f"알 수 없는 위험등급: {grade!r}") from exc
