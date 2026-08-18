"""M-03 모듈 분리 스모크 테스트.

app.py에서 뷰 함수들을 medsupply/views/* 모듈로 옮긴 뒤에도 각 뷰가 예외 없이
렌더되는지 확인한다. AppTest.from_function()에 넘기는 함수는 자기 완결적이어야
하므로(inspect.getsourcelines로 함수 본문만 추출해 별도 스크립트로 실행됨),
모든 import를 함수 본문 안에 둔다.

뷰 함수는 st.navigation을 거치지 않고 직접 호출되므로 AppTest와 st.navigation의
충돌 가능성을 피한다(브리프의 BLOCKED 조건 미해당).
"""

from __future__ import annotations

from streamlit.testing.v1 import AppTest


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


def _run_notices() -> None:
    from medsupply import theme
    from medsupply.views import notices

    theme.inject_css()
    notices.render()


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


def test_situation_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_situation)
    at.run()
    assert not at.exception


def test_situation_view_has_at_least_one_button() -> None:
    """구조 확인용 얕은 단언: 상황실 뷰에는 '검토 대기 4건 확인' 버튼이 있다."""
    at = AppTest.from_function(_run_situation)
    at.run()
    assert not at.exception
    assert len(at.button) >= 1


def test_review_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_review)
    at.run()
    assert not at.exception


def test_orders_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_orders)
    at.run()
    assert not at.exception


def test_history_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_history)
    at.run()
    assert not at.exception


def test_notices_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_notices)
    at.run()
    assert not at.exception


def test_alerts_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_alerts)
    at.run()
    assert not at.exception


def test_evaluation_view_runs_without_exception() -> None:
    at = AppTest.from_function(_run_evaluation)
    at.run()
    assert not at.exception
