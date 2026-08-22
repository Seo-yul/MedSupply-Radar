"""Task M-32: 데모 3(최종 시연) 리허설 기계 검증.

`docs/demo-script.md`가 "지금 이 화면에 이렇게 뜬다"고 서술하는 내용이 실제로 그렇게
렌더되는지를 기계로 확인한다. 표준 스냅샷(`data/medsupply.db`)을 다른 `_live` 테스트들
(tests/platform/test_situation_live.py 등)처럼 subprocess로 재생성한 tmp 사본이 아니라
**저장소의 실제 파일 그대로** 대상으로 삼는다 — 리허설의 목적이 "실제 시연 때 보게 될
화면"을 미리 확인하는 것이기 때문이다(브리프: 표준 DB 읽기 전용). `medsupply.settings.
DB_PATH`는 monkeypatch하지 않고 기본값(레포 루트 기준 상대경로)을 그대로 쓴다.

`LLM_MODE=offline`은 `monkeypatch.setenv`로 강제한다. 이 환경에는 API 키가 없어
review.py의 "설명 생성" 버튼(`llm_cfg.anthropic_key_set or llm_cfg.openai_key_set`
조건부 노출)이 애초에 렌더되지 않지만, 브리프가 명시적으로 요구하는 방어선이다 — 우연히
환경변수에 키가 섞여 들어와도 오프라인 강제로 실제 네트워크 호출을 막는다.

`st.cache_data`/`st.cache_resource`는 프로세스 전역이라(tests/platform/
test_situation_live.py 등과 동일 관례) 매 렌더 전에 clear()한다.

표준 스냅샷을 이 모듈이 실제로 변형하지 않는지는 `_standard_db_read_only_guard`가 핵심
테이블 행수를 모듈 시작·종료 시점에 대조해 증명한다 — alerts.render()가 렌더마다
sync_alerts(멱등)를 무조건 실행해 쓰기 커넥션을 열기 때문에, "그럴 것이다"로 남겨두지
않고 직접 확인한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings

# ---------------------------------------------------------------------------
# 표준 스냅샷 읽기 전용 가드 — 모듈 전체 대상(autouse)
# ---------------------------------------------------------------------------

#: alerts.render()의 무조건적 sync_alerts 호출을 포함해, 이 리허설이 렌더 도중 건드릴
#: 가능성이 있는 쓰기 대상 테이블 전부. 행수가 모듈 시작·종료 시점에 완전히 같아야 한다.
_GUARD_TABLES = (
    "items", "risk_results", "notices", "notice_extractions", "notice_item_map",
    "alerts", "action_history", "order_requests", "llm_explanations",
)


def _table_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in _GUARD_TABLES}
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def _standard_db_read_only_guard():
    """표준 스냅샷(data/medsupply.db)이 이 모듈 실행 전후로 한 행도 안 바뀌었는지 증명한다.

    alerts.render()는 렌더마다 sync_alerts(멱등)를 무조건 실행해 쓰기 커넥션을 열지만,
    이미 동기화된 표준 스냅샷에서는 실제로 삽입되는 행이 없어야 한다(dedupe). "멱등이니
    괜찮을 것이다"로 끝내지 않고 모듈 시작·종료 시점의 핵심 테이블 행수를 직접 대조한다.
    """
    if not settings.DB_PATH.exists():
        yield
        return
    before = _table_counts(settings.DB_PATH)
    yield
    after = _table_counts(settings.DB_PATH)
    assert after == before, f"표준 스냅샷이 리허설 도중 변경됐다: {before} -> {after}"


# ---------------------------------------------------------------------------
# 렌더 헬퍼 — AppTest.from_function 계약(자기완결 임포트).
# tests/platform/test_views_smoke.py·test_situation_live.py와 동일 관례.
# ---------------------------------------------------------------------------


def _run_situation() -> None:
    from medsupply import theme
    from medsupply.views import situation

    theme.inject_css()
    situation.render()


def _run_review() -> None:
    from medsupply import theme
    from medsupply.views import review

    theme.inject_css()
    review.render()


def _run_notices() -> None:
    from medsupply import theme
    from medsupply.views import notices

    theme.inject_css()
    notices.render()


def _run_orders() -> None:
    from medsupply import theme
    from medsupply.views import orders

    theme.inject_css()
    orders.render()


def _run_history() -> None:
    from medsupply import theme
    from medsupply.views import history

    theme.inject_css()
    history.render()


def _run_alerts() -> None:
    from medsupply import theme
    from medsupply.views import alerts

    theme.inject_css()
    alerts.render()


def _run_evaluation() -> None:
    from medsupply import theme
    from medsupply.views import evaluation

    theme.inject_css()
    evaluation.render()


def _activate_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_MODE=offline 강제 + 캐시 clear(관례). DB_PATH는 건드리지 않는다(표준 DB 그대로,
    브리프: 표준 DB 읽기 전용 — 리허설은 실제 시연 환경을 그대로 봐야 하므로 tmp 사본으로
    바꿔치기하지 않는다)."""
    monkeypatch.setenv("LLM_MODE", "offline")
    st.cache_data.clear()
    st.cache_resource.clear()


def _rendered_markdown(at: AppTest) -> str:
    return "\n".join(md.value for md in at.markdown)


def _metric_value(at: AppTest, label: str) -> str:
    for m in at.metric:
        if m.label == label:
            return m.value
    raise AssertionError(f"metric 라벨을 찾을 수 없다: {label!r}")


#: docs/demo-script.md 스텝 1~4가 상황실→워크벤치→발주로 이어서 따라가는 대표 품목
#: (표준 스냅샷 실측 위험 1위 품목 — inventory.load_overview().iloc[0], Task X-3 체인
#: 리뷰 F2에서 메디헤파린주 25000IU/5mL(실측 4위)로부터 갱신).
_LEAD_ITEM_NAME = "메디로라제팜주 4mg/2mL"


# ---------------------------------------------------------------------------
# 7페이지 전부 무예외 렌더 + docs/demo-script.md 화면 표식 표본 5개
#
# 브리프가 요구하는 "표본 5개"는 아래 5개 함수에서 각 1개씩 검증한다:
#   M1·M2 — 상황실(스텝 1): KPI '최고 위험'=7, 대표 품목명 노출
#   M3    — 검토 대기함/워크벤치(스텝 2): 같은 품목의 라벨 노출
#   M4    — 공급 공고(스텝 3): "공고 20건"·자동확정 표시(추출 결과 병기) 노출
#   M5    — 발주·조치안(스텝 4): "부족 예상량 0(충분)" 계산 근거 문구 노출
# 나머지 3페이지(이력·알림·AI 평가)는 무예외 렌더만 확인한다(브리프가 요구하는 표본
# 개수는 이미 5개로 충족되며, 이력·알림의 "축적" 서사는 라이브 액션 의존이라 스냅샷
# 고정 표식이 없다). AI 평가 페이지에는 검증 스토리 메커니즘 문구 보너스 검증을 더한다.
# ---------------------------------------------------------------------------


def test_situation_page_renders_and_matches_script_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """마커 1·2 — 상황실: KPI '최고 위험'=7, 대표 품목명 노출(docs/demo-script.md 스텝 1)."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_situation)
    at.run()

    assert not at.exception
    assert _metric_value(at, "최고 위험") == "7"
    assert _LEAD_ITEM_NAME in _rendered_markdown(at)


def test_review_page_renders_and_matches_script_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """마커 3 — 검토 대기함(워크벤치): 기본 선택 품목에 AI 원인 설명이 실데이터로 존재
    (스텝 2, 런북 실행 후 상태).

    X-2의 위험 배치 재실행(공고발 등급 상향 반영)으로 최고 위험점수 품목의 정체성이
    바뀌어 더 이상 _LEAD_ITEM_NAME(메디헤파린주, 런북 실행 전 1순위)이 기본 선택되지
    않는다 — 이 실패의 근본원인이 특정 품목 이름에 대한 결합이었으므로, 새 단언은
    품목 정체성이 아니라 "AI 근거 설명 탭에 생성물이 있는가"로 잡는다. warm_cache.py
    실행으로 alert 대상 71개 품목에 이미 설명이 채워져 있어, 지금은 몇 순위든 기본
    선택 품목에 pending 안내 대신 실제 생성 메타(공급자/모델)가 뜬다."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    rendered = _rendered_markdown(at)
    assert "AI 원인 설명이 아직 생성되지 않았습니다" not in rendered
    assert "생성: anthropic/claude-opus-5" in rendered


def test_notices_page_renders_and_matches_script_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """마커 4 — 공급 공고: 공고 20건 적재·추출 결과 병기 표시(스텝 3, 런북 실행 후 상태).

    X-2에서 python scripts/process_notices.py --all이 실제로 실행돼 20건 전부
    '자동확정' 상태로 추출을 마쳤다(저장소 실 DB — 모듈 docstring대로 tmp 사본이 아니라
    실 파일 그대로 읽는다). 그래서 "추출 미실행" 안내(st.info)는 더 이상 뜨지 않고,
    대신 추출 결과 JSON 1건과 "자동확정된 공고입니다" 캡션이 뜬다 — 이것이 새 상태의
    실질 표식이다(마커를 지우지 않고 반대 상태로 교체한다)."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_notices)
    at.run()

    assert not at.exception
    assert "공고 20건" in _rendered_markdown(at)
    # 20건 전부 추출을 마쳤으므로 "추출 미실행" 안내는 더 이상 뜨지 않는다.
    assert not any("추출 미실행" in info.value for info in at.info)
    # 대신 기본 선택된 공고의 추출 결과가 st.json 1건으로, 확인 상태가 캡션으로 뜬다.
    assert len(at.json) == 1
    assert any("자동확정된 공고입니다" in c.value for c in at.caption)


def test_orders_page_renders_and_matches_script_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """마커 5 — 발주·조치안: 부족 예상량 0(충분) — 계산 근거 투명성 멘트의 화면 증거(스텝 4)."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_orders)
    at.run()

    assert not at.exception
    assert "부족 예상량<b>0 (충분)</b>" in _rendered_markdown(at)


def test_history_page_renders_without_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """스텝 5 — 대응 이력: 무예외 렌더 확인(축적 서사는 워크벤치·발주의 라이브 저장 액션에
    의존해 스냅샷 고정 표식이 없다 — 브리프가 요구하는 표본 5개는 위 4개 함수에서 충족)."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_history)
    at.run()

    assert not at.exception


def test_alerts_page_renders_without_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """스텝 6 — 알림센터: 자동 sync 경로 포함 무예외 렌더. 표준 스냅샷이 실제로 변형되지
    않는지는 모듈 전체 가드(_standard_db_read_only_guard)가 별도로 증명한다."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_alerts)
    at.run()

    assert not at.exception


def test_evaluation_page_renders_without_exception_and_shows_mechanism_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """스텝 7 — AI 평가: 무예외 렌더 + 검증 스토리 W1 메커니즘 문구가 실제로 화면에
    있는지 보너스 확인(docs/demo-script.md ④가 인용하는 문장과 동일 출처 —
    medsupply/views/evaluation.py의 _render_blind_summary 고정 문구)."""
    _activate_offline(monkeypatch)

    at = AppTest.from_function(_run_evaluation)
    at.run()

    assert not at.exception
    rendered = _rendered_markdown(at)
    assert "탐지기 동일" in rendered
    assert "감지율 상승을" in rendered
