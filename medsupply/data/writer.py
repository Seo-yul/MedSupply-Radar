"""쓰기 단일 경로 — 배치 적재·LLM 영속화·화면 저장의 모든 쓰기가 경유하는 유일한 모듈.

이 모듈 밖에서 INSERT/UPDATE/DELETE를 하지 않는다(읽기는 queries.py, 쓰기는 이 모듈).
모든 공개 함수는 성공 시 meta.data_version을 정확히 1 증가시킨다(Streamlit
st.cache_data 무효화 신호로 쓰인다). 각 공개 함수는 자체 트랜잭션(``with conn:``)으로
원자적이다 — 본문이 실패(ValueError·미확인 IntegrityError 등)하면 data_version도
증가하지 않는다.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime

import pandas as pd

_VALID_GRADES = {"위험", "경고", "주의", "정상"}
_VALID_NOTICE_STATUS = {"자동확정", "확인 필요", "확인 완료"}
_VALID_ACTION_STATUS = {"진행 중", "완료"}
_VALID_SEVERITY = {"긴급", "높음", "확인"}
_VALID_ACTION_RISK_TYPES = {
    "demand_surge", "supply_halt", "delivery_delay", "composite", "general",
}


def _validate_choice(value: str, valid: set[str], field: str) -> None:
    """value가 valid 밖이면 명확한 메시지의 ValueError를 던진다(DB CHECK 위반보다 먼저)."""
    if value not in valid:
        raise ValueError(f"invalid {field} {value!r} — must be one of {sorted(valid)}")


def _none_if_nan(value: object) -> object:
    """pandas가 결측을 NaN으로 채운 값을 SQL NULL(None)로 되돌린다."""
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _maybe_json_dumps(value: object, expected_type: type) -> object:
    """value가 expected_type이면 json.dumps, 아니면(이미 문자열 등) 그대로 둔다."""
    if isinstance(value, expected_type):
        return json.dumps(value, ensure_ascii=False)
    return value


def _bump_data_version(conn: sqlite3.Connection) -> None:
    """meta의 'data_version'을 정수 증가시킨다(없으면 1로 생성).

    호출부(각 공개 함수)의 ``with conn:`` 블록 내부에서, 커밋 직전에 호출해야 한다.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('data_version', '1')")
    else:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'data_version'",
            (str(int(row["value"]) + 1),),
        )


def save_risk_results(
    conn: sqlite3.Connection, results: pd.DataFrame, run_id: str, as_of: date
) -> None:
    """쓰기 단일 경로 — data_version 증가

    results 필수 컬럼: item_id, grade, base_grade, escalated_by_notice, risk_type,
    score, days_to_stockout, depletion_date, factors_json(dict면 json.dumps). grade·
    base_grade가 유효 등급 집합({'위험','경고','주의','정상'}) 밖이면 ValueError. 같은
    run_id로 재저장하면 해당 run_id 행 전체를 DELETE 후 INSERT한다(멱등). as_of는 ISO
    문자열로 저장한다.
    """
    records = results.to_dict(orient="records")
    for record in records:
        _validate_choice(record["grade"], _VALID_GRADES, "grade")
        _validate_choice(record["base_grade"], _VALID_GRADES, "base_grade")

    as_of_str = as_of.isoformat()

    with conn:
        conn.execute("DELETE FROM risk_results WHERE run_id = ?", (run_id,))
        for record in records:
            conn.execute(
                "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
                " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date,"
                " factors_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    record["item_id"],
                    as_of_str,
                    record["grade"],
                    record["base_grade"],
                    int(record["escalated_by_notice"]),
                    record["risk_type"],
                    _none_if_nan(record["score"]),
                    _none_if_nan(record["days_to_stockout"]),
                    _none_if_nan(record["depletion_date"]),
                    _maybe_json_dumps(record["factors_json"], dict),
                ),
            )
        _bump_data_version(conn)


def save_forecasts(
    conn: sqlite3.Connection, forecasts: pd.DataFrame, run_id: str, as_of: date
) -> None:
    """쓰기 단일 경로 — data_version 증가

    forecasts 필수 컬럼: item_id, horizon_days, avg_daily_forecast, total_forecast,
    daily_json(list면 json.dumps). 같은 run_id로 재저장하면 해당 run_id 행 전체를 DELETE
    후 INSERT한다(멱등).
    """
    records = forecasts.to_dict(orient="records")
    as_of_str = as_of.isoformat()

    with conn:
        conn.execute("DELETE FROM forecasts WHERE run_id = ?", (run_id,))
        for record in records:
            conn.execute(
                "INSERT INTO forecasts(run_id, item_id, as_of, horizon_days,"
                " avg_daily_forecast, total_forecast, daily_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    record["item_id"],
                    as_of_str,
                    int(record["horizon_days"]),
                    _none_if_nan(record["avg_daily_forecast"]),
                    _none_if_nan(record["total_forecast"]),
                    _maybe_json_dumps(record["daily_json"], list),
                ),
            )
        _bump_data_version(conn)


def save_notice_extraction(
    conn: sqlite3.Connection,
    notice_id: str,
    payload: dict,
    confidence: float,
    status: str,
    prompt_version: str,
    provider: str,
    model: str,
    mapped: list[dict],
) -> None:
    """쓰기 단일 경로 — data_version 증가

    notice_extractions에 INSERT OR REPLACE(notice_id PK)한다. status가
    {'자동확정','확인 필요','확인 완료'} 밖이면 ValueError. payload는
    json.dumps(ensure_ascii=False)로 저장한다. mapped 원소는 {item_id,
    substitute_group_id, match_basis, needs_review} 형태이며, 해당 notice_id의 기존
    notice_item_map 행을 DELETE한 뒤 일괄 INSERT한다(멱등).
    """
    _validate_choice(status, _VALID_NOTICE_STATUS, "status")

    payload_json = json.dumps(payload, ensure_ascii=False)

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO notice_extractions"
            " (notice_id, payload_json, confidence, status, prompt_version, provider, model)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (notice_id, payload_json, confidence, status, prompt_version, provider, model),
        )
        conn.execute("DELETE FROM notice_item_map WHERE notice_id = ?", (notice_id,))
        for m in mapped:
            conn.execute(
                "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id,"
                " match_basis, needs_review) VALUES (?, ?, ?, ?, ?)",
                (
                    notice_id,
                    m["item_id"],
                    m["substitute_group_id"],
                    m["match_basis"],
                    int(m["needs_review"]),
                ),
            )
        _bump_data_version(conn)


def save_explanation(
    conn: sqlite3.Connection,
    item_id: str,
    payload: dict,
    prompt_version: str,
    provider: str,
    model: str,
    run_id: str,
) -> None:
    """쓰기 단일 경로 — data_version 증가

    llm_explanations에 INSERT OR REPLACE(item_id PK)한다. generated_at은 호출 시각
    (datetime.now().isoformat(timespec='seconds'))으로 채운다 — 이 값은 표시용일 뿐
    판정·측정에는 쓰이지 않는다.
    """
    payload_json = json.dumps(payload, ensure_ascii=False)
    generated_at = datetime.now().isoformat(timespec="seconds")

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO llm_explanations"
            " (item_id, run_id, payload_json, prompt_version, provider, model, generated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (item_id, run_id, payload_json, prompt_version, provider, model, generated_at),
        )
        _bump_data_version(conn)


def save_action_history(
    conn: sqlite3.Connection,
    item_id: str,
    action_type: str,
    owner: str,
    note: str,
    status: str = "진행 중",
    order_id: int | None = None,
    risk_type: str | None = None,
) -> int:
    """쓰기 단일 경로 — data_version 증가

    action_history에 INSERT하고 실제 rowid(cur.lastrowid)를 반환한다. created_at은
    호출 시각(datetime.now().isoformat(timespec='seconds'))으로 채운다. status가
    {'진행 중','완료'} 밖이면 ValueError. risk_type은 기존 호출부 하위호환을 위한
    keyword 전용 인자로, 생략하면 NULL로 저장된다. 값을 줄 경우
    {'demand_surge','supply_halt','delivery_delay','composite','general'} 밖이면
    ValueError(M-21 이력 참조용, v1.1 승인 변경).
    """
    _validate_choice(status, _VALID_ACTION_STATUS, "status")
    if risk_type is not None:
        _validate_choice(risk_type, _VALID_ACTION_RISK_TYPES, "risk_type")

    created_at = datetime.now().isoformat(timespec="seconds")

    with conn:
        cur = conn.execute(
            "INSERT INTO action_history(created_at, item_id, action_type, owner, note,"
            " status, order_id, risk_type) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (created_at, item_id, action_type, owner, note, status, order_id, risk_type),
        )
        history_id = cur.lastrowid
        _bump_data_version(conn)
    return history_id


def save_order_request(
    conn: sqlite3.Connection,
    item_id: str,
    supplier: str,
    quantity: int,
    desired_date: str,
    owner: str,
    reason: str,
) -> int:
    """쓰기 단일 경로 — data_version 증가

    order_requests에 INSERT하고 실제 rowid(cur.lastrowid)를 반환한다. created_at은
    호출 시각(datetime.now().isoformat(timespec='seconds'))으로 채운다.
    """
    created_at = datetime.now().isoformat(timespec="seconds")

    with conn:
        cur = conn.execute(
            "INSERT INTO order_requests(created_at, item_id, supplier, quantity,"
            " desired_date, owner, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (created_at, item_id, supplier, quantity, desired_date, owner, reason),
        )
        order_id = cur.lastrowid
        _bump_data_version(conn)
    return order_id


def create_alert(
    conn: sqlite3.Connection,
    alert_type: str,
    item_id: str | None,
    title: str,
    body: str | None,
    severity: str,
    dedupe_key: str,
) -> int | None:
    """쓰기 단일 경로 — data_version 증가

    alerts에 INSERT하고 실제 rowid(cur.lastrowid)를 반환한다. severity가
    {'긴급','높음','확인'} 밖이면 ValueError. dedupe_key UNIQUE 충돌 시에는 예외를
    삼키고 None을 반환한다(dedupe_key와 무관한 다른 IntegrityError는 그대로 전파한다).
    is_read는 스키마 기본값(0)을 그대로 쓴다.
    """
    _validate_choice(severity, _VALID_SEVERITY, "severity")

    created_at = datetime.now().isoformat(timespec="seconds")

    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO alerts(created_at, alert_type, item_id, title, body,"
                " severity, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, alert_type, item_id, title, body, severity, dedupe_key),
            )
            alert_id = cur.lastrowid
            _bump_data_version(conn)
    except sqlite3.IntegrityError as exc:
        if "dedupe_key" in str(exc):
            return None
        raise
    return alert_id


def mark_alert_read(conn: sqlite3.Connection, alert_id: int) -> None:
    """쓰기 단일 경로 — data_version 증가

    alerts.is_read를 1로 갱신한다.
    """
    with conn:
        conn.execute("UPDATE alerts SET is_read = 1 WHERE alert_id = ?", (alert_id,))
        _bump_data_version(conn)


def set_notice_status(conn: sqlite3.Connection, notice_id: str, status: str) -> None:
    """쓰기 단일 경로 — data_version 증가

    notice_extractions.status를 갱신한다. status가 {'자동확정','확인 필요','확인 완료'}
    밖이면 ValueError. 존재하지 않는 notice_id면 ValueError.
    """
    _validate_choice(status, _VALID_NOTICE_STATUS, "status")

    with conn:
        cur = conn.execute(
            "UPDATE notice_extractions SET status = ? WHERE notice_id = ?",
            (status, notice_id),
        )
        if cur.rowcount == 0:
            raise ValueError(f"unknown notice_id: {notice_id!r}")
        _bump_data_version(conn)
