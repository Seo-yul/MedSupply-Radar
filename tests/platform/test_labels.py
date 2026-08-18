"""Task M-04: 위험등급 명칭 통일 회귀 테스트.

두 축을 혼동하지 않는다(자세한 배경은 태스크 브리프 참조):
- 위험등급(이 파일의 검사 대상): 위험/경고/주의/정상. 금지어는 구 명칭 중
  '매우 높음'과 '관찰' 두 가지뿐이다. '높음'·'안정'은 공급상태·알림 심각도·
  일반 서술 등 다른 의미로 저장소에 남아있는 것이 정상이므로 검사하지 않는다.
- 공급상태 라벨(불변): 현재 품절/품절 예상/공급중단/정상화.
- 알림 심각도(불변): 긴급/높음/확인.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medsupply.ui.grades import GRADE_ORDER, grade_css
from medsupply.views._demo import DRUGS

REPO_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()

_BANNED_GRADE_TERMS = ("매우 높음", "관찰")


def _source_files() -> list[Path]:
    """medsupply/ 아래 모든 .py 파일 + app.py. 이 테스트 파일 자신은 제외."""
    files = sorted((REPO_ROOT / "medsupply").rglob("*.py"))
    app_py = REPO_ROOT / "app.py"
    if app_py.is_file():
        files.append(app_py)
    return [f for f in files if f.resolve() != _THIS_FILE]


def test_grade_order_matches_plan_standard() -> None:
    assert GRADE_ORDER == ["위험", "경고", "주의", "정상"]


@pytest.mark.parametrize(
    ("grade", "css_class"),
    [
        ("위험", "critical"),
        ("경고", "high"),
        ("주의", "watch"),
        ("정상", "safe"),
    ],
)
def test_grade_css_maps_all_four_grades(grade: str, css_class: str) -> None:
    assert grade_css(grade) == css_class


def test_grade_css_rejects_unknown_grade() -> None:
    with pytest.raises(ValueError):
        grade_css("매우 높음")


@pytest.mark.parametrize("term", _BANNED_GRADE_TERMS)
def test_banned_old_grade_terms_absent_from_source(term: str) -> None:
    """구 위험등급 명칭('매우 높음'/'관찰')은 medsupply/·app.py 어디에도 남지 않는다."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in _source_files()
        if term in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"금지어 '{term}'가 남아있는 파일: {offenders}"


def test_demo_drugs_grades_are_within_grade_order() -> None:
    """_demo.py DRUGS의 위험등급 열 값은 전부 GRADE_ORDER 안에 있어야 한다."""
    grades = set(DRUGS["위험등급"].unique())
    assert grades, "DRUGS에 위험등급 값이 없다"
    assert grades.issubset(set(GRADE_ORDER))
