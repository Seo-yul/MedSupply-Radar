"""medsupply/data/queries.py(읽기 전용 조회 계층) 계약 검증.

화면·분석·측정·LLM 전 도메인이 공용으로 소비하는 조회 함수 14종을 고정한다. 어떤 함수도
INSERT/UPDATE/DELETE를 하지 않는다 — 이 파일은 각 함수의 대표 경로, 필터 최소 1개, 빈 결과
경로를 함께 검증한다.

픽스처(fixture_conn/empty_conn)와 시드 데이터 상수는 tests/conftest.py 참조.
"""

from __future__ import annotations

import math
from datetime import date

import pandas as pd
import pytest

from medsupply.data import queries
from tests.conftest import (
    AS_OF_TODAY,
    INGREDIENT_1,
    INGREDIENT_2,
    ITEM_1,
    ITEM_2,
    ITEM_3,
    NOTICE_HALT,
    NOTICE_NORMALIZED,
    RUN_TODAY,
    RUN_YESTERDAY,
    SUBSTITUTE_GROUP_1,
)


def _is_null(value: object) -> bool:
    """pandas가 NaN/None으로 표현하는 SQL NULL을 판별한다."""
    return value is None or (isinstance(value, float) and math.isnan(value))


# --- list_items ----------------------------------------------------------------

LIST_ITEMS_COLUMNS = [
    "item_id",
    "item_name",
    "ingredient_code",
    "ingredient_name_kr",
    "strength",
    "form",
    "route",
    "supplier",
    "is_essential",
    "substitute_group_id",
    "grade",
    "score",
    "days_to_stockout",
    "risk_type",
]


def test_list_items_default_uses_latest_run_and_has_contract_columns(fixture_conn) -> None:
    df = queries.list_items(fixture_conn)

    assert list(df.columns) == LIST_ITEMS_COLUMNS
    assert list(df["item_id"]) == [ITEM_1, ITEM_2, ITEM_3]

    today_row = df[df["item_id"] == ITEM_1].iloc[0]
    assert today_row["grade"] == "위험"
    assert today_row["score"] == 92
    assert today_row["days_to_stockout"] == 5
    assert today_row["risk_type"] == "supply_halt"

    no_run_row = df[df["item_id"] == ITEM_2].iloc[0]
    assert _is_null(no_run_row["grade"])


def test_list_items_explicit_run_id_uses_that_run_not_latest(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, run_id=RUN_YESTERDAY)

    row = df[df["item_id"] == ITEM_1].iloc[0]
    assert row["grade"] == "경고"
    assert row["score"] == 55


def test_list_items_filter_by_ingredient_code(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, ingredient_code=INGREDIENT_1)
    assert set(df["item_id"]) == {ITEM_1, ITEM_2, ITEM_3}


def test_list_items_filter_by_ingredient_code_with_no_items_is_empty(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, ingredient_code=INGREDIENT_2)
    assert df.empty


def test_list_items_essential_only(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, essential_only=True)
    assert list(df["item_id"]) == [ITEM_1]


def test_list_items_search_matches_korean_ingredient_name(fixture_conn) -> None:
    """'나트륨'은 ingredient_name_kr에만 있고 item_name에는 없다 — 성분명 검색 분기 검증."""
    df = queries.list_items(fixture_conn, search="나트륨")
    assert set(df["item_id"]) == {ITEM_1, ITEM_2, ITEM_3}


def test_list_items_search_matches_english_ingredient_name_case_insensitive(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, search="sodium")
    assert set(df["item_id"]) == {ITEM_1, ITEM_2, ITEM_3}


def test_list_items_search_matches_item_name_precisely(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, search="500mg")
    assert list(df["item_id"]) == [ITEM_3]


def test_list_items_search_no_match_returns_empty(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, search="아스피린")
    assert df.empty


def test_list_items_grade_filter(fixture_conn) -> None:
    df = queries.list_items(fixture_conn, grade="위험")
    assert list(df["item_id"]) == [ITEM_1]


def test_list_items_no_runs_at_all_leaves_risk_columns_null(empty_conn) -> None:
    """run이 아예 없으면 위험 컬럼은 NULL이어야 한다(품목 목록 자체는 비지 않는다)."""
    conn = empty_conn
    conn.execute(
        "INSERT INTO items(item_id, item_name) VALUES ('ITEM-X', '테스트품목')"
    )
    conn.commit()

    df = queries.list_items(conn)

    assert list(df["item_id"]) == ["ITEM-X"]
    assert _is_null(df.iloc[0]["grade"])


# --- get_item --------------------------------------------------------------


def test_get_item_returns_joined_dict(fixture_conn) -> None:
    item = queries.get_item(fixture_conn, ITEM_1)

    assert item["item_id"] == ITEM_1
    assert item["item_name"] == "세프트리악손주 1g(한국제약)"
    assert item["ingredient_code"] == INGREDIENT_1
    assert item["ingredient_name_kr"] == "세프트리악손나트륨"
    assert item["ingredient_name_en"] == "Ceftriaxone Sodium"
    assert item["substitute_group_id"] == SUBSTITUTE_GROUP_1
    assert item["is_essential"] == 1


def test_get_item_unknown_id_raises_keyerror(fixture_conn) -> None:
    with pytest.raises(KeyError, match="NO-SUCH-ITEM"):
        queries.get_item(fixture_conn, "NO-SUCH-ITEM")


# --- get_daily_series --------------------------------------------------------


def test_get_daily_series_returns_all_rows_ordered_by_date(fixture_conn) -> None:
    df = queries.get_daily_series(fixture_conn, ITEM_1)

    assert list(df.columns) == ["date", "usage_qty", "incoming_qty", "closing_stock"]
    assert list(df["date"]) == ["2026-07-30", "2026-07-31", "2026-08-01"]
    assert list(df["closing_stock"]) == [100, 88, 80]


def test_get_daily_series_filters_by_start_and_end(fixture_conn) -> None:
    df = queries.get_daily_series(
        fixture_conn, ITEM_1, start=date(2026, 7, 31), end=date(2026, 7, 31)
    )
    assert list(df["date"]) == ["2026-07-31"]


def test_get_daily_series_range_outside_data_returns_empty(fixture_conn) -> None:
    df = queries.get_daily_series(fixture_conn, ITEM_1, start=date(2026, 9, 1))
    assert df.empty


# --- get_substitutes ----------------------------------------------------------


def test_get_substitutes_same_condition_only_excludes_self_and_other_groups(fixture_conn) -> None:
    df = queries.get_substitutes(fixture_conn, ITEM_1)

    assert list(df["item_id"]) == [ITEM_2]
    assert bool(df.iloc[0]["same_condition"]) is True
    assert df.iloc[0]["current_stock"] == 40  # ITEM_2의 2026-08-01 closing_stock


def test_get_substitutes_all_includes_other_group_same_ingredient(fixture_conn) -> None:
    df = queries.get_substitutes(fixture_conn, ITEM_1, same_condition_only=False)

    by_item = {row["item_id"]: row for _, row in df.iterrows()}
    assert set(by_item) == {ITEM_2, ITEM_3}
    assert bool(by_item[ITEM_2]["same_condition"]) is True
    assert bool(by_item[ITEM_3]["same_condition"]) is False
    assert by_item[ITEM_3]["current_stock"] == 52  # ITEM_3의 2026-08-01 closing_stock


def test_get_substitutes_no_siblings_in_own_group_returns_empty(fixture_conn) -> None:
    """ITEM_3은 자신의 대체군(SG-2)에 다른 품목이 없다."""
    df = queries.get_substitutes(fixture_conn, ITEM_3)
    assert df.empty


# --- get_incoming_shipments ---------------------------------------------------


def test_get_incoming_shipments_default_returns_pending_shipment(fixture_conn) -> None:
    df = queries.get_incoming_shipments(fixture_conn)
    assert list(df["item_id"]) == [ITEM_1]
    assert df.iloc[0]["actual_date"] is None or _is_null(df.iloc[0]["actual_date"])


def test_get_incoming_shipments_filter_by_item_id(fixture_conn) -> None:
    df = queries.get_incoming_shipments(fixture_conn, ITEM_1)
    assert len(df) == 1
    assert df.iloc[0]["item_id"] == ITEM_1


def test_get_incoming_shipments_item_without_shipments_is_empty(fixture_conn) -> None:
    df = queries.get_incoming_shipments(fixture_conn, ITEM_3)
    assert df.empty


def test_get_incoming_shipments_pending_only_excludes_completed(empty_conn) -> None:
    """공유 픽스처는 미입고 1건뿐이라 배제 동작 자체는 별도 최소 데이터로 검증한다."""
    conn = empty_conn
    conn.execute("INSERT INTO items(item_id, item_name) VALUES ('ITEM-X', '테스트품목')")
    conn.executemany(
        "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
        " actual_date, actual_qty, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("ITEM-X", "2026-07-01", "2026-07-10", 100, "2026-07-10", 100, "입고완료"),
            ("ITEM-X", "2026-07-20", "2026-08-05", 200, None, None, "예정"),
        ],
    )
    conn.commit()

    pending = queries.get_incoming_shipments(conn, "ITEM-X", pending_only=True)
    everything = queries.get_incoming_shipments(conn, "ITEM-X", pending_only=False)

    assert len(pending) == 1
    assert pending.iloc[0]["status"] == "예정"
    assert len(everything) == 2


# --- get_notices ---------------------------------------------------------------


def test_get_notices_default_lists_both_with_status_and_mapped_count(fixture_conn) -> None:
    df = queries.get_notices(fixture_conn)

    assert list(df["notice_id"]) == [NOTICE_NORMALIZED, NOTICE_HALT]  # published_date DESC

    by_id = {row["notice_id"]: row for _, row in df.iterrows()}
    assert by_id[NOTICE_HALT]["status"] == "확인 필요"
    assert by_id[NOTICE_HALT]["mapped_count"] == 2
    assert by_id[NOTICE_NORMALIZED]["status"] == "자동확정"
    assert by_id[NOTICE_NORMALIZED]["mapped_count"] == 0


def test_get_notices_filter_by_status(fixture_conn) -> None:
    df = queries.get_notices(fixture_conn, status="자동확정")
    assert list(df["notice_id"]) == [NOTICE_NORMALIZED]


def test_get_notices_filter_by_item_id(fixture_conn) -> None:
    df = queries.get_notices(fixture_conn, item_id=ITEM_1)
    assert list(df["notice_id"]) == [NOTICE_HALT]


def test_get_notices_item_with_no_mapping_returns_empty(fixture_conn) -> None:
    df = queries.get_notices(fixture_conn, item_id=ITEM_3)
    assert df.empty


def test_get_notices_includes_confidence_from_extraction(fixture_conn) -> None:
    df = queries.get_notices(fixture_conn)
    by_id = {row["notice_id"]: row for _, row in df.iterrows()}
    assert by_id[NOTICE_HALT]["confidence"] == pytest.approx(0.6)
    assert by_id[NOTICE_NORMALIZED]["confidence"] == pytest.approx(0.95)


# --- get_notice_detail -----------------------------------------------------


def test_get_notice_detail_returns_notice_with_payload_and_mapped_items(fixture_conn) -> None:
    detail = queries.get_notice_detail(fixture_conn, NOTICE_HALT)

    assert detail is not None
    assert detail["title"] == "세프트리악손주 공급중단 안내"
    assert detail["raw_text"] == "제조소 사정으로 2026년 7월 15일부터 공급이 중단됩니다."
    assert detail["status"] == "확인 필요"
    assert detail["confidence"] == pytest.approx(0.6)
    assert detail["payload"]["reason"] == "제조소 설비 점검"
    assert [m["item_id"] for m in detail["mapped"]] == [ITEM_1, ITEM_2]
    assert detail["mapped"][1]["needs_review"] == 1


def test_get_notice_detail_notice_without_extraction_has_none_payload(fixture_conn) -> None:
    fixture_conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, notice_type)"
        " VALUES ('NTC-0003', '2026-07-20', '미추출 공고', '기타')"
    )
    fixture_conn.commit()

    detail = queries.get_notice_detail(fixture_conn, "NTC-0003")

    assert detail is not None
    assert detail["payload"] is None
    assert detail["status"] is None
    assert detail["confidence"] is None
    assert detail["mapped"] == []


def test_get_notice_detail_unknown_notice_id_returns_none(fixture_conn) -> None:
    assert queries.get_notice_detail(fixture_conn, "NO-SUCH-NOTICE") is None


# --- get_active_notice_map -----------------------------------------------------


def test_get_active_notice_map_includes_null_restart_excludes_normalized(fixture_conn) -> None:
    df = queries.get_active_notice_map(fixture_conn, date(2026, 8, 1))

    assert list(df.columns) == [
        "notice_id",
        "item_id",
        "substitute_group_id",
        "needs_review",
        "notice_type",
        "expected_restart_date",
    ]
    assert set(df["notice_id"]) == {NOTICE_HALT}
    assert set(df["item_id"]) == {ITEM_1, ITEM_2}
    for value in df["expected_restart_date"]:
        assert _is_null(value)


def test_get_active_notice_map_excludes_restart_date_before_as_of(empty_conn) -> None:
    """공유 픽스처의 유일한 '공급중단' 공고는 재개예정일이 NULL이라, 날짜 배제 로직은
    별도 최소 데이터(재개예정일이 과거인 공급중단 공고)로 독립 검증한다."""
    conn = empty_conn
    conn.execute(
        "INSERT INTO items(item_id, item_name) VALUES ('ITEM-X', '테스트품목')"
    )
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, notice_type)"
        " VALUES ('NTC-X', '2026-01-01', '과거 공급중단', '공급중단')"
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, status)"
        " VALUES ('NTC-X', ?, '자동확정')",
        ('{"expected_restart_date": "2026-01-01"}',),
    )
    conn.execute(
        "INSERT INTO notice_item_map(notice_id, item_id) VALUES ('NTC-X', 'ITEM-X')"
    )
    conn.commit()

    df = queries.get_active_notice_map(conn, date(2026, 8, 1))

    assert df.empty


def test_get_active_notice_map_empty_when_no_notices(empty_conn) -> None:
    df = queries.get_active_notice_map(empty_conn, date(2026, 8, 1))
    assert df.empty


# --- get_latest_runs ------------------------------------------------------------


def test_get_latest_runs_orders_by_as_of_desc(fixture_conn) -> None:
    assert queries.get_latest_runs(fixture_conn) == [RUN_TODAY, RUN_YESTERDAY]


def test_get_latest_runs_respects_n(fixture_conn) -> None:
    assert queries.get_latest_runs(fixture_conn, n=1) == [RUN_TODAY]


def test_get_latest_runs_empty_when_no_runs(empty_conn) -> None:
    assert queries.get_latest_runs(empty_conn) == []


# --- get_risk_results ------------------------------------------------------------


def test_get_risk_results_returns_rows_for_run(fixture_conn) -> None:
    df = queries.get_risk_results(fixture_conn, RUN_TODAY)
    assert list(df["item_id"]) == [ITEM_1]
    assert df.iloc[0]["grade"] == "위험"


def test_get_risk_results_unknown_run_returns_empty(fixture_conn) -> None:
    df = queries.get_risk_results(fixture_conn, "NO-SUCH-RUN")
    assert df.empty


# --- get_forecast ------------------------------------------------------------


def test_get_forecast_returns_dict_with_daily_list(fixture_conn) -> None:
    forecast = queries.get_forecast(fixture_conn, RUN_TODAY, ITEM_1)

    assert forecast is not None
    assert forecast["daily"] == [9, 10, 9, 10, 9]
    assert forecast["horizon_days"] == 5
    assert forecast["avg_daily_forecast"] == pytest.approx(9.6)
    assert "daily_json" not in forecast


def test_get_forecast_missing_combo_returns_none(fixture_conn) -> None:
    assert queries.get_forecast(fixture_conn, RUN_YESTERDAY, ITEM_1) is None


# --- list_action_history ------------------------------------------------------


def test_list_action_history_orders_latest_first(fixture_conn) -> None:
    df = queries.list_action_history(fixture_conn)
    assert list(df["item_id"]) == [ITEM_2, ITEM_1]  # ITEM_2가 더 최근 created_at


def test_list_action_history_filter_by_item_id(fixture_conn) -> None:
    df = queries.list_action_history(fixture_conn, item_id=ITEM_1)
    assert list(df["item_id"]) == [ITEM_1]


def test_list_action_history_filter_by_ingredient_code_matches_via_items_join(
    fixture_conn,
) -> None:
    df = queries.list_action_history(fixture_conn, ingredient_code=INGREDIENT_1)
    assert set(df["item_id"]) == {ITEM_1, ITEM_2}


def test_list_action_history_ingredient_code_with_no_history_is_empty(fixture_conn) -> None:
    df = queries.list_action_history(fixture_conn, ingredient_code=INGREDIENT_2)
    assert df.empty


def test_list_action_history_limit(fixture_conn) -> None:
    df = queries.list_action_history(fixture_conn, limit=1)
    assert list(df["item_id"]) == [ITEM_2]


def test_list_action_history_risk_type_argument_does_not_raise(fixture_conn) -> None:
    """v1 미지원 — 인자를 받기만 하고 예외 없이 동작해야 한다(브리프 명시)."""
    df = queries.list_action_history(fixture_conn, risk_type="demand_surge")
    assert isinstance(df, pd.DataFrame)


# --- fetch_alerts ------------------------------------------------------------


def test_fetch_alerts_default_orders_latest_first(fixture_conn) -> None:
    df = queries.fetch_alerts(fixture_conn)
    assert list(df["alert_type"]) == ["risk_escalation", "notice_new"]


def test_fetch_alerts_unread_only(fixture_conn) -> None:
    df = queries.fetch_alerts(fixture_conn, unread_only=True)
    assert list(df["alert_type"]) == ["risk_escalation"]
    assert list(df["is_read"]) == [0]


def test_fetch_alerts_limit_zero_returns_empty(fixture_conn) -> None:
    df = queries.fetch_alerts(fixture_conn, limit=0)
    assert df.empty


# --- get_meta ------------------------------------------------------------------


def test_get_meta_returns_all_keys(fixture_conn) -> None:
    meta = queries.get_meta(fixture_conn)
    assert meta == {
        "seed": "20260819",
        "base_date": AS_OF_TODAY,
        "item_count": "3",
        "data_version": "1",
    }


def test_get_meta_empty_when_no_rows(empty_conn) -> None:
    assert queries.get_meta(empty_conn) == {}


# --- 계약: 읽기 전용(정적 검사) --------------------------------------------------


def test_queries_module_source_has_no_write_statements() -> None:
    """queries.py는 어떤 함수도 INSERT/UPDATE/DELETE를 하지 않는다(브리프 명시)."""
    import inspect as _inspect

    source = _inspect.getsource(queries).upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert verb not in source, f"queries.py에 쓰기 구문이 있으면 안 된다: {verb}"
