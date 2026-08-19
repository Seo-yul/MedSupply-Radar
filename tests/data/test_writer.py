"""medsupply/data/writer.py(쓰기 단일 경로) 계약 검증.

저장소의 모든 쓰기(배치 적재·LLM 영속화·화면 저장)는 이 모듈을 경유해야 한다는 규약을
고정한다. 모든 공개 함수는 성공 시 meta.data_version을 정확히 1 증가시키고, 실패(ValueError
등) 시에는 증가시키지 않는다(각 함수 자체 트랜잭션의 원자성).

픽스처(fixture_conn/empty_conn)와 시드 데이터 상수는 tests/conftest.py 참조.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd
import pytest

from medsupply.data import queries, writer
from tests.conftest import (
    ITEM_1,
    ITEM_2,
    ITEM_3,
    NOTICE_HALT,
    RUN_TODAY,
    RUN_YESTERDAY,
    SUBSTITUTE_GROUP_2,
)


def _data_version(conn: sqlite3.Connection) -> int:
    return int(queries.get_meta(conn)["data_version"])


def _risk_row(
    item_id: str,
    *,
    grade: str = "위험",
    base_grade: str = "위험",
    score: int = 80,
    factors_json: object = "{}",
) -> dict:
    return {
        "item_id": item_id,
        "grade": grade,
        "base_grade": base_grade,
        "escalated_by_notice": 0,
        "risk_type": "general",
        "score": score,
        "days_to_stockout": 5,
        "depletion_date": "2026-08-10",
        "factors_json": factors_json,
    }


def _forecast_row(item_id: str, *, total: float = 50.0, daily_json: object = None) -> dict:
    return {
        "item_id": item_id,
        "horizon_days": 7,
        "avg_daily_forecast": total / 7,
        "total_forecast": total,
        "daily_json": daily_json if daily_json is not None else [1, 2, 3, 4, 5, 6, 7],
    }


# --- 공통 계약: docstring ------------------------------------------------------


def test_all_public_functions_document_data_version_bump() -> None:
    """모든 공개 함수 docstring 첫 줄은 '쓰기 단일 경로 — data_version 증가'다."""
    public_funcs = [
        writer.save_risk_results,
        writer.save_forecasts,
        writer.save_notice_extraction,
        writer.save_explanation,
        writer.save_action_history,
        writer.save_order_request,
        writer.create_alert,
        writer.mark_alert_read,
        writer.set_notice_status,
    ]
    for func in public_funcs:
        assert func.__doc__, func.__name__
        first_line = func.__doc__.strip().splitlines()[0].strip()
        assert first_line == "쓰기 단일 경로 — data_version 증가", func.__name__


def test_bump_data_version_creates_key_when_missing(empty_conn: sqlite3.Connection) -> None:
    """meta에 data_version 키가 아예 없으면 1로 생성한다."""
    assert "data_version" not in queries.get_meta(empty_conn)

    writer.create_alert(
        empty_conn, "system", None, "제목", "본문", "확인", "dedupe-empty-1"
    )

    assert queries.get_meta(empty_conn)["data_version"] == "1"


# --- save_risk_results -----------------------------------------------------


def test_save_risk_results_success_bumps_data_version_by_one(fixture_conn) -> None:
    before = _data_version(fixture_conn)
    df = pd.DataFrame([_risk_row(ITEM_1)])

    writer.save_risk_results(fixture_conn, df, "RUN-NEW-1", date(2026, 8, 2))

    assert _data_version(fixture_conn) == before + 1


def test_save_risk_results_stores_dict_factors_json_as_text(fixture_conn) -> None:
    df = pd.DataFrame([_risk_row(ITEM_1, factors_json={"reason": "test"})])

    writer.save_risk_results(fixture_conn, df, "RUN-NEW-2", date(2026, 8, 2))

    stored = queries.get_risk_results(fixture_conn, "RUN-NEW-2")
    assert json.loads(stored.iloc[0]["factors_json"]) == {"reason": "test"}


def test_save_risk_results_stores_as_of_as_iso_string(fixture_conn) -> None:
    df = pd.DataFrame([_risk_row(ITEM_1)])

    writer.save_risk_results(fixture_conn, df, "RUN-NEW-3", date(2026, 8, 3))

    stored = queries.get_risk_results(fixture_conn, "RUN-NEW-3")
    assert stored.iloc[0]["as_of"] == "2026-08-03"


def test_save_risk_results_invalid_grade_raises_value_error(fixture_conn) -> None:
    df = pd.DataFrame([_risk_row(ITEM_1, grade="매우 높음")])

    with pytest.raises(ValueError):
        writer.save_risk_results(fixture_conn, df, "RUN-BAD-1", date(2026, 8, 2))


def test_save_risk_results_invalid_base_grade_raises_value_error(fixture_conn) -> None:
    df = pd.DataFrame([_risk_row(ITEM_1, base_grade="알수없음")])

    with pytest.raises(ValueError):
        writer.save_risk_results(fixture_conn, df, "RUN-BAD-2", date(2026, 8, 2))


def test_save_risk_results_invalid_grade_does_not_write_or_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)
    df = pd.DataFrame([_risk_row(ITEM_1, grade="매우 높음")])

    with pytest.raises(ValueError):
        writer.save_risk_results(fixture_conn, df, "RUN-BAD-3", date(2026, 8, 2))

    assert _data_version(fixture_conn) == before
    assert queries.get_risk_results(fixture_conn, "RUN-BAD-3").empty


def test_save_risk_results_same_run_id_is_idempotent(fixture_conn) -> None:
    run_id = "RUN-IDEMPOTENT-1"
    as_of = date(2026, 8, 2)
    df1 = pd.DataFrame([_risk_row(ITEM_1, score=50), _risk_row(ITEM_2, score=20)])
    writer.save_risk_results(fixture_conn, df1, run_id, as_of)

    df2 = pd.DataFrame([_risk_row(ITEM_1, score=99), _risk_row(ITEM_2, score=5)])
    writer.save_risk_results(fixture_conn, df2, run_id, as_of)

    stored = queries.get_risk_results(fixture_conn, run_id)
    assert len(stored) == 2
    row1 = stored[stored["item_id"] == ITEM_1].iloc[0]
    assert row1["score"] == 99


# --- save_forecasts ----------------------------------------------------------


def test_save_forecasts_success_bumps_version_and_round_trips_daily_json(fixture_conn) -> None:
    before = _data_version(fixture_conn)
    df = pd.DataFrame([_forecast_row(ITEM_1)])

    writer.save_forecasts(fixture_conn, df, "FCAST-NEW-1", date(2026, 8, 2))

    assert _data_version(fixture_conn) == before + 1
    stored = queries.get_forecast(fixture_conn, "FCAST-NEW-1", ITEM_1)
    assert stored is not None
    assert stored["daily"] == [1, 2, 3, 4, 5, 6, 7]


def test_save_forecasts_same_run_id_is_idempotent(fixture_conn) -> None:
    run_id = "FCAST-IDEMPOTENT-1"
    as_of = date(2026, 8, 2)
    writer.save_forecasts(fixture_conn, pd.DataFrame([_forecast_row(ITEM_1, total=50.0)]), run_id, as_of)
    writer.save_forecasts(fixture_conn, pd.DataFrame([_forecast_row(ITEM_1, total=77.0)]), run_id, as_of)

    count = fixture_conn.execute(
        "SELECT COUNT(*) FROM forecasts WHERE run_id = ?", (run_id,)
    ).fetchone()[0]
    assert count == 1
    stored = queries.get_forecast(fixture_conn, run_id, ITEM_1)
    assert stored["total_forecast"] == 77.0


# --- save_notice_extraction --------------------------------------------------


def test_save_notice_extraction_success_bumps_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    writer.save_notice_extraction(
        fixture_conn,
        notice_id=NOTICE_HALT,
        payload={"a": 1},
        confidence=0.8,
        status="확인 완료",
        prompt_version="v2",
        provider="anthropic",
        model="claude-x",
        mapped=[
            {
                "item_id": ITEM_3,
                "substitute_group_id": SUBSTITUTE_GROUP_2,
                "match_basis": "standard_code",
                "needs_review": 0,
            }
        ],
    )

    assert _data_version(fixture_conn) == before + 1
    row = fixture_conn.execute(
        "SELECT * FROM notice_extractions WHERE notice_id = ?", (NOTICE_HALT,)
    ).fetchone()
    assert row["status"] == "확인 완료"
    assert json.loads(row["payload_json"]) == {"a": 1}


def test_save_notice_extraction_replaces_existing_mapping(fixture_conn) -> None:
    """재저장 시 기존 notice_item_map 행이 제거되고 새 매핑으로 교체된다(멱등)."""
    before_item1 = queries.get_notices(fixture_conn, item_id=ITEM_1)
    assert NOTICE_HALT in before_item1["notice_id"].tolist()

    writer.save_notice_extraction(
        fixture_conn,
        notice_id=NOTICE_HALT,
        payload={"a": 1},
        confidence=0.9,
        status="확인 완료",
        prompt_version="v2",
        provider="anthropic",
        model="claude-x",
        mapped=[
            {
                "item_id": ITEM_3,
                "substitute_group_id": SUBSTITUTE_GROUP_2,
                "match_basis": "standard_code",
                "needs_review": 0,
            }
        ],
    )

    after_item1 = queries.get_notices(fixture_conn, item_id=ITEM_1)
    assert NOTICE_HALT not in after_item1["notice_id"].tolist()
    after_item3 = queries.get_notices(fixture_conn, item_id=ITEM_3)
    assert NOTICE_HALT in after_item3["notice_id"].tolist()


def test_save_notice_extraction_invalid_status_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.save_notice_extraction(
            fixture_conn, NOTICE_HALT, {"a": 1}, 0.5, "확인불가", "v1", "anthropic", "m", []
        )


def test_save_notice_extraction_invalid_status_does_not_write_or_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)
    before_item1 = queries.get_notices(fixture_conn, item_id=ITEM_1)
    assert NOTICE_HALT in before_item1["notice_id"].tolist()

    with pytest.raises(ValueError):
        writer.save_notice_extraction(
            fixture_conn, NOTICE_HALT, {"a": 1}, 0.5, "확인불가", "v1", "anthropic", "m", []
        )

    assert _data_version(fixture_conn) == before
    after_item1 = queries.get_notices(fixture_conn, item_id=ITEM_1)
    assert NOTICE_HALT in after_item1["notice_id"].tolist()


# --- save_explanation ---------------------------------------------------------


def test_save_explanation_success_bumps_version_and_stores_payload(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    writer.save_explanation(
        fixture_conn, ITEM_1, {"summary": "위험 상향"}, "explain@v1", "anthropic", "claude-x", RUN_TODAY
    )

    assert _data_version(fixture_conn) == before + 1
    row = fixture_conn.execute(
        "SELECT * FROM llm_explanations WHERE item_id = ?", (ITEM_1,)
    ).fetchone()
    assert row is not None
    assert json.loads(row["payload_json"]) == {"summary": "위험 상향"}
    assert row["generated_at"]


def test_save_explanation_replaces_existing_row_for_same_item(fixture_conn) -> None:
    writer.save_explanation(
        fixture_conn, ITEM_1, {"summary": "첫번째"}, "v1", "anthropic", "m1", RUN_YESTERDAY
    )
    writer.save_explanation(
        fixture_conn, ITEM_1, {"summary": "두번째"}, "v2", "anthropic", "m2", RUN_TODAY
    )

    count = fixture_conn.execute(
        "SELECT COUNT(*) FROM llm_explanations WHERE item_id = ?", (ITEM_1,)
    ).fetchone()[0]
    assert count == 1
    row = fixture_conn.execute(
        "SELECT payload_json FROM llm_explanations WHERE item_id = ?", (ITEM_1,)
    ).fetchone()
    assert json.loads(row["payload_json"]) == {"summary": "두번째"}


# --- save_action_history ------------------------------------------------------


def test_save_action_history_returns_actual_rowid(fixture_conn) -> None:
    history_id = writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모")

    row = fixture_conn.execute(
        "SELECT * FROM action_history WHERE history_id = ?", (history_id,)
    ).fetchone()
    assert row is not None
    assert row["item_id"] == ITEM_1
    assert row["status"] == "진행 중"


def test_save_action_history_bumps_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모")

    assert _data_version(fixture_conn) == before + 1


def test_save_action_history_invalid_status_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모", status="대기")


def test_save_action_history_invalid_status_does_not_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    with pytest.raises(ValueError):
        writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모", status="대기")

    assert _data_version(fixture_conn) == before


def test_save_action_history_without_risk_type_stores_null(fixture_conn) -> None:
    """risk_type 미지정 시 하위호환 — NULL로 저장된다(기존 호출부 영향 없음)."""
    history_id = writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모")

    row = fixture_conn.execute(
        "SELECT risk_type FROM action_history WHERE history_id = ?", (history_id,)
    ).fetchone()
    assert row["risk_type"] is None


def test_save_action_history_with_valid_risk_type_round_trips(fixture_conn) -> None:
    history_id = writer.save_action_history(
        fixture_conn, ITEM_1, "대체 검토", "약사A", "메모", risk_type="supply_halt"
    )

    row = fixture_conn.execute(
        "SELECT risk_type FROM action_history WHERE history_id = ?", (history_id,)
    ).fetchone()
    assert row["risk_type"] == "supply_halt"


def test_save_action_history_invalid_risk_type_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.save_action_history(
            fixture_conn, ITEM_1, "대체 검토", "약사A", "메모", risk_type="알수없음"
        )


def test_save_action_history_invalid_risk_type_does_not_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    with pytest.raises(ValueError):
        writer.save_action_history(
            fixture_conn, ITEM_1, "대체 검토", "약사A", "메모", risk_type="알수없음"
        )

    assert _data_version(fixture_conn) == before


# --- save_order_request --------------------------------------------------------


def test_save_order_request_returns_rowid_and_bumps_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    order_id = writer.save_order_request(
        fixture_conn, ITEM_1, "한국제약", 100, "2026-08-15", "약사A", "긴급 소진 임박"
    )

    row = fixture_conn.execute(
        "SELECT * FROM order_requests WHERE order_id = ?", (order_id,)
    ).fetchone()
    assert row is not None
    assert row["quantity"] == 100
    assert _data_version(fixture_conn) == before + 1


# --- create_alert ---------------------------------------------------------------


def test_create_alert_returns_id_and_bumps_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    alert_id = writer.create_alert(
        fixture_conn, "risk_escalation", ITEM_2, "제목", "본문", "높음", "dedupe-new-1"
    )

    assert isinstance(alert_id, int)
    assert _data_version(fixture_conn) == before + 1


def test_create_alert_duplicate_dedupe_key_returns_none_and_row_count_unchanged(fixture_conn) -> None:
    dedupe_key = "dedupe-dup-1"
    first_id = writer.create_alert(
        fixture_conn, "risk_escalation", ITEM_2, "제목", "본문", "높음", dedupe_key
    )
    assert first_id is not None
    count_before = fixture_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    version_before = _data_version(fixture_conn)

    result = writer.create_alert(
        fixture_conn, "risk_escalation", ITEM_2, "제목2", "본문2", "긴급", dedupe_key
    )

    assert result is None
    assert fixture_conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == count_before
    assert _data_version(fixture_conn) == version_before


def test_create_alert_invalid_severity_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.create_alert(
            fixture_conn, "risk_escalation", ITEM_2, "제목", "본문", "매우높음", "dedupe-bad-1"
        )


def test_create_alert_invalid_severity_does_not_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    with pytest.raises(ValueError):
        writer.create_alert(
            fixture_conn, "risk_escalation", ITEM_2, "제목", "본문", "매우높음", "dedupe-bad-2"
        )

    assert _data_version(fixture_conn) == before


def test_create_alert_non_dedupe_integrity_error_propagates(fixture_conn) -> None:
    """dedupe_key와 무관한 무결성 오류(존재하지 않는 item_id의 FK 위반)는 전파돼야 한다."""
    with pytest.raises(sqlite3.IntegrityError):
        writer.create_alert(
            fixture_conn, "risk_escalation", "NO-SUCH-ITEM", "제목", "본문", "확인", "dedupe-fk-1"
        )


# --- mark_alert_read -------------------------------------------------------------


def test_mark_alert_read_sets_is_read_and_bumps_version(fixture_conn) -> None:
    unread = queries.fetch_alerts(fixture_conn, unread_only=True)
    assert len(unread) >= 1
    alert_id = int(unread.iloc[0]["alert_id"])
    before = _data_version(fixture_conn)

    writer.mark_alert_read(fixture_conn, alert_id)

    after_unread = queries.fetch_alerts(fixture_conn, unread_only=True)
    assert alert_id not in after_unread["alert_id"].tolist()
    assert _data_version(fixture_conn) == before + 1


# --- set_notice_status -------------------------------------------------------------


def test_set_notice_status_updates_and_bumps_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    writer.set_notice_status(fixture_conn, NOTICE_HALT, "확인 완료")

    row = fixture_conn.execute(
        "SELECT status FROM notice_extractions WHERE notice_id = ?", (NOTICE_HALT,)
    ).fetchone()
    assert row["status"] == "확인 완료"
    assert _data_version(fixture_conn) == before + 1


def test_set_notice_status_unknown_notice_id_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.set_notice_status(fixture_conn, "NO-SUCH-NOTICE", "확인 완료")


def test_set_notice_status_unknown_notice_id_does_not_bump_version(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    with pytest.raises(ValueError):
        writer.set_notice_status(fixture_conn, "NO-SUCH-NOTICE", "확인 완료")

    assert _data_version(fixture_conn) == before


def test_set_notice_status_invalid_status_raises_value_error(fixture_conn) -> None:
    with pytest.raises(ValueError):
        writer.set_notice_status(fixture_conn, NOTICE_HALT, "보류")


# --- 누적 검증 --------------------------------------------------------------------


def test_data_version_accumulates_across_multiple_writes(fixture_conn) -> None:
    before = _data_version(fixture_conn)

    writer.save_action_history(fixture_conn, ITEM_1, "대체 검토", "약사A", "메모")
    writer.save_order_request(fixture_conn, ITEM_1, "공급사", 10, "2026-08-10", "약사A", "긴급")
    writer.create_alert(fixture_conn, "risk_escalation", ITEM_1, "제목", "본문", "확인", "dedupe-accum-1")

    assert _data_version(fixture_conn) == before + 3
