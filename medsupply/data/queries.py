"""읽기 전용 조회 계층 — 화면·분석·측정·LLM 전 도메인이 공용으로 소비한다.

이 모듈의 어떤 함수도 INSERT/UPDATE/DELETE를 하지 않는다(쓰기는 writer.py의 몫이다).
모든 함수는 결정적 정렬(ORDER BY 명시)로 반환한다.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pandas as pd


def list_items(
    conn: sqlite3.Connection,
    *,
    ingredient_code: str | None = None,
    form: str | None = None,
    supplier: str | None = None,
    grade: str | None = None,
    essential_only: bool = False,
    search: str | None = None,
    run_id: str | None = None,
) -> pd.DataFrame:
    """items ⨝ ingredients LEFT JOIN risk_results(최신/지정 run) 목록.

    run_id가 None이면 get_latest_runs(conn, 1)의 최신 run을 쓴다(run이 없으면 위험 관련
    컬럼은 전부 NULL). search는 item_name·ingredient_name_kr·ingredient_name_en 부분일치
    (대소문자 무시)다. grade 필터는 risk_results.grade와 일치하는 행만 남긴다.
    """
    if run_id is None:
        latest = get_latest_runs(conn, 1)
        run_id = latest[0] if latest else None

    query = """
        SELECT
            i.item_id,
            i.item_name,
            i.ingredient_code,
            ing.ingredient_name_kr,
            i.strength,
            i.form,
            i.route,
            i.supplier,
            i.is_essential,
            i.substitute_group_id,
            r.grade,
            r.score,
            r.days_to_stockout,
            r.risk_type
        FROM items AS i
        LEFT JOIN ingredients AS ing ON ing.ingredient_code = i.ingredient_code
        LEFT JOIN risk_results AS r ON r.item_id = i.item_id AND r.run_id = :run_id
        WHERE 1 = 1
    """
    params: dict[str, object] = {"run_id": run_id}

    if ingredient_code is not None:
        query += " AND i.ingredient_code = :ingredient_code"
        params["ingredient_code"] = ingredient_code
    if form is not None:
        query += " AND i.form = :form"
        params["form"] = form
    if supplier is not None:
        query += " AND i.supplier = :supplier"
        params["supplier"] = supplier
    if grade is not None:
        query += " AND r.grade = :grade"
        params["grade"] = grade
    if essential_only:
        query += " AND i.is_essential = 1"
    if search is not None:
        query += (
            " AND ("
            " LOWER(i.item_name) LIKE :search"
            " OR LOWER(ing.ingredient_name_kr) LIKE :search"
            " OR LOWER(ing.ingredient_name_en) LIKE :search"
            " )"
        )
        params["search"] = f"%{search.lower()}%"

    query += " ORDER BY i.item_id"

    return pd.read_sql_query(query, conn, params=params)


def get_item(conn: sqlite3.Connection, item_id: str) -> dict:
    """items ⨝ ingredients 1행을 dict로 반환한다. 없으면 KeyError."""
    row = conn.execute(
        """
        SELECT i.*, ing.ingredient_name_kr, ing.ingredient_name_en
        FROM items AS i
        LEFT JOIN ingredients AS ing ON ing.ingredient_code = i.ingredient_code
        WHERE i.item_id = ?
        """,
        (item_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown item_id: {item_id}")
    return dict(row)


def get_daily_series(
    conn: sqlite3.Connection,
    item_id: str,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """stock_usage_daily 시계열. columns: date, usage_qty, incoming_qty, closing_stock."""
    query = (
        "SELECT date, usage_qty, incoming_qty, closing_stock"
        " FROM stock_usage_daily WHERE item_id = :item_id"
    )
    params: dict[str, object] = {"item_id": item_id}

    if start is not None:
        query += " AND date >= :start"
        params["start"] = str(start)
    if end is not None:
        query += " AND date <= :end"
        params["end"] = str(end)

    query += " ORDER BY date"
    return pd.read_sql_query(query, conn, params=params)


def get_current_stock_map(conn: sqlite3.Connection) -> pd.DataFrame:
    """전 품목의 최신(날짜 기준) closing_stock 일괄 조회(단일 SQL, 품목별 반복 조회 없음).

    stock_usage_daily에서 품목별 최신 date 1행의 closing_stock만 남긴다. 반환 컬럼:
    item_id, current_stock. 시계열이 없는 품목은 결과에 나타나지 않는다(호출부가 좌측
    조인으로 NULL을 채운다 — 예: services.inventory.load_overview).
    """
    query = """
        SELECT s.item_id, s.closing_stock AS current_stock
        FROM stock_usage_daily AS s
        INNER JOIN (
            SELECT item_id, MAX(date) AS max_date
            FROM stock_usage_daily
            GROUP BY item_id
        ) AS latest
            ON latest.item_id = s.item_id AND latest.max_date = s.date
        ORDER BY s.item_id
    """
    return pd.read_sql_query(query, conn)


def get_substitutes(
    conn: sqlite3.Connection,
    item_id: str,
    *,
    same_condition_only: bool = True,
    as_of: date | None = None,
) -> pd.DataFrame:
    """같은 대체군(및 옵션에 따라 같은 성분의 타 대체군) 품목 목록.

    same_condition_only=True(기본): 소스 품목과 같은 substitute_group_id의 다른 품목만.
    False: 같은 ingredient_code를 갖는 타 대체군 품목도 포함하고, same_condition 불리언
    컬럼으로 두 집합을 구분한다. current_stock은 각 품목의 stock_usage_daily 중 최신 date의
    closing_stock이며 기록이 없으면 NULL이다.

    as_of(선택, 기본 None): 지정하면 "최신"을 date <= as_of 범위로 제한한다 — 과거 run
    기준으로 대체 후보 재고를 조회할 때(medsupply.llm.grounding) as_of 이후 기록을
    미래 정보로 끌어오는 룩어헤드를 막기 위함이다(리뷰 F4). None(기본)이면 기존과 동일하게
    전체 기간 중 최신 1건이다 — 기존 호출부(services.workbench)는 이 인자를 넘기지 않으므로
    동작이 그대로 보존된다.
    """
    source = get_item(conn, item_id)
    group_id = source["substitute_group_id"]
    ingredient_code = source["ingredient_code"]

    where_clause = "i.substitute_group_id = :group_id"
    params: dict[str, object] = {"item_id": item_id, "group_id": group_id}
    if not same_condition_only:
        where_clause += (
            " OR (i.ingredient_code = :ingredient_code"
            " AND (i.substitute_group_id IS NULL OR i.substitute_group_id != :group_id))"
        )
        params["ingredient_code"] = ingredient_code

    stock_date_filter = ""
    if as_of is not None:
        stock_date_filter = " AND s.date <= :as_of"
        params["as_of"] = str(as_of)

    query = f"""
        SELECT
            i.item_id,
            i.item_name,
            i.ingredient_code,
            i.strength,
            i.form,
            i.route,
            i.supplier,
            i.substitute_group_id,
            CASE WHEN i.substitute_group_id = :group_id THEN 1 ELSE 0 END AS same_condition,
            (
                SELECT s.closing_stock FROM stock_usage_daily AS s
                WHERE s.item_id = i.item_id{stock_date_filter}
                ORDER BY s.date DESC
                LIMIT 1
            ) AS current_stock
        FROM items AS i
        WHERE i.item_id != :item_id AND ({where_clause})
        ORDER BY same_condition DESC, i.item_id
    """
    df = pd.read_sql_query(query, conn, params=params)
    df["same_condition"] = df["same_condition"].astype(bool)
    return df


def get_incoming_shipments(
    conn: sqlite3.Connection, item_id: str | None = None, *, pending_only: bool = True
) -> pd.DataFrame:
    """입고 예정/실적 목록. pending_only=True(기본)면 actual_date가 NULL인(미입고) 건만."""
    query = "SELECT * FROM incoming_shipments WHERE 1 = 1"
    params: dict[str, object] = {}

    if item_id is not None:
        query += " AND item_id = :item_id"
        params["item_id"] = item_id
    if pending_only:
        query += " AND actual_date IS NULL"

    query += " ORDER BY expected_date, shipment_id"
    return pd.read_sql_query(query, conn, params=params)


def get_notices(
    conn: sqlite3.Connection, *, item_id: str | None = None, status: str | None = None
) -> pd.DataFrame:
    """공고 목록. extraction 상태(status)·신뢰도(confidence)와 매핑 품목 수(mapped_count)를
    조인한다.

    status는 notice_extractions.status 필터(추출이 없는 공고는 status·confidence가 모두
    NULL). item_id는 해당 품목에 매핑된 공고만 남긴다(mapped_count는 필터와 무관하게 공고의
    전체 매핑 수).
    """
    query = """
        SELECT
            n.notice_id,
            n.published_date,
            n.title,
            n.source,
            n.source_url,
            n.notice_type,
            n.collected_at,
            e.status,
            e.confidence,
            COUNT(DISTINCT m.item_id) AS mapped_count
        FROM notices AS n
        LEFT JOIN notice_extractions AS e ON e.notice_id = n.notice_id
        LEFT JOIN notice_item_map AS m ON m.notice_id = n.notice_id
        WHERE 1 = 1
    """
    params: dict[str, object] = {}

    if item_id is not None:
        query += (
            " AND n.notice_id IN ("
            " SELECT notice_id FROM notice_item_map WHERE item_id = :item_id"
            " )"
        )
        params["item_id"] = item_id
    if status is not None:
        query += " AND e.status = :status"
        params["status"] = status

    query += " GROUP BY n.notice_id ORDER BY n.published_date DESC, n.notice_id"
    return pd.read_sql_query(query, conn, params=params)


def get_notice_detail(conn: sqlite3.Connection, notice_id: str) -> dict | None:
    """공고 1건 상세 — notices 1행(raw_text 포함) + notice_extractions LEFT JOIN + 매핑 목록.

    payload_json은 json.loads 해 payload 키로 반환한다. 추출이 없으면 payload는 None이고
    confidence·status·prompt_version·provider·model·created_at도 전부 None이다(LEFT JOIN
    미매치를 그대로 표현). mapped: notice_item_map × items 조인 리스트
    [{item_id, item_name, substitute_group_id, match_basis, needs_review}]를 item_id
    오름차순으로 담는다. notice_id가 존재하지 않으면 None.
    """
    notice_row = conn.execute(
        "SELECT * FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    if notice_row is None:
        return None
    detail = dict(notice_row)

    extraction_row = conn.execute(
        "SELECT payload_json, confidence, status, prompt_version, provider, model, created_at"
        " FROM notice_extractions WHERE notice_id = ?",
        (notice_id,),
    ).fetchone()
    if extraction_row is None:
        detail.update(
            payload=None, confidence=None, status=None,
            prompt_version=None, provider=None, model=None, created_at=None,
        )
    else:
        extraction = dict(extraction_row)
        extraction["payload"] = json.loads(extraction.pop("payload_json"))
        detail.update(extraction)

    mapped_rows = conn.execute(
        """
        SELECT m.item_id, i.item_name, m.substitute_group_id, m.match_basis, m.needs_review
        FROM notice_item_map AS m
        JOIN items AS i ON i.item_id = m.item_id
        WHERE m.notice_id = ?
        ORDER BY m.item_id
        """,
        (notice_id,),
    ).fetchall()
    detail["mapped"] = [dict(row) for row in mapped_rows]

    return detail


def get_active_notice_map(conn: sqlite3.Connection, as_of: date) -> pd.DataFrame:
    """활성 공고 매핑(docs/data-model.md §2.4).

    활성 = notices.notice_type가 '공급중단'/'공급부족'이고, **published_date가 as_of
    이하**이며(as_of 시점에는 아직 게시되지 않았을 미래 공고가 활성 목록에 끌려 들어오는
    룩어헤드를 차단한다 — 2주차 브랜치 리뷰 F4, 표준 스냅샷은 전 공고가 스윕 이전에 게시돼
    현재 데이터로는 무영향이지만 as_of를 과거로 돌리는 재측정에서 의미가 생긴다),
    payload_json의 expected_restart_date가 NULL이거나 as_of 이상. payload_json 파싱은
    SQLite json_extract를 쓴다. notice_item_map은 notices·notice_extractions와 내부
    조인한다 — 활성 여부 판정에 반드시 필요한 추출 데이터가 없는 매핑은 판정 불가로 보고
    제외한다.
    """
    query = """
        SELECT
            m.notice_id,
            m.item_id,
            m.substitute_group_id,
            m.needs_review,
            n.notice_type,
            json_extract(e.payload_json, '$.expected_restart_date') AS expected_restart_date
        FROM notice_item_map AS m
        JOIN notices AS n ON n.notice_id = m.notice_id
        JOIN notice_extractions AS e ON e.notice_id = m.notice_id
        WHERE n.notice_type IN ('공급중단', '공급부족')
          AND n.published_date <= :as_of
          AND (
              json_extract(e.payload_json, '$.expected_restart_date') IS NULL
              OR json_extract(e.payload_json, '$.expected_restart_date') >= :as_of
          )
        ORDER BY m.notice_id, m.item_id
    """
    return pd.read_sql_query(query, conn, params={"as_of": str(as_of)})


def get_latest_runs(conn: sqlite3.Connection, n: int = 2) -> list[str]:
    """risk_results의 run_id 중, **최신 run과 같은 params_hash 패밀리**만 as_of 내림차순
    (동률 시 run_id 내림차순)으로 최대 n개 반환한다.

    규칙(2주차 브랜치 리뷰 F1): "최신"은 전체 run 중 as_of 내림차순(동률 시 run_id 내림차순)
    1순위를 말한다. "패밀리"는 run_id의 '#' 뒤 부분(현재 형식은 8자 params_hash prefix —
    run_id = f"{as_of}#{params_hash[:8]}", scripts/run_risk_batch.py). 최신 run과 패밀리가
    다른 run(예: 같은 as_of에 파라미터를 바꿔 재실행해 남은 구 run)은 as_of가 아무리 최근이어도
    제외한다 — 그렇지 않으면 이 함수를 소비하는 화면(상황실 KPI delta, 워크벤치 prev_risk)이
    "전일 대비"가 아니라 "파라미터 변경 대비"를 보여주는 사고가 난다. 판정은 **자기 일관적**이다
    — config 파일 값을 조회 조건으로 결합하지 않고, 오직 "이 조회 시점의 최신 run이 속한
    패밀리"만 기준으로 삼는다. 패밀리가 다른 구 run은 DB에서 지우지 않는다(보존) — 이 함수의
    반환 목록에서만 제외된다.

    가드(재리뷰 마이크로 픽스): '#' 포함 형식은 run_risk_batch.py만의 관례일 뿐 writer.py가
    강제하지 않는다 — 최신 run_id에 '#'이 없으면 패밀리 개념 자체가 없으므로, 그 run_id와
    완전히 같은 값(자기 자신만의 패밀리)만 반환한다. partition을 써서 '#'이 없어도
    IndexError로 죽지 않는다.
    """
    latest_row = conn.execute(
        """
        SELECT run_id
        FROM risk_results
        ORDER BY as_of DESC, run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if latest_row is None:
        return []

    latest_run_id = latest_row["run_id"]
    _, sep, family = latest_run_id.partition("#")

    if sep:
        where_clause = "substr(run_id, instr(run_id, '#') + 1) = ?"
        family_param = family
    else:
        where_clause = "run_id = ?"
        family_param = latest_run_id

    rows = conn.execute(
        f"""
        SELECT run_id, MAX(as_of) AS as_of
        FROM risk_results
        WHERE {where_clause}
        GROUP BY run_id
        ORDER BY as_of DESC, run_id DESC
        LIMIT ?
        """,
        (family_param, n),
    ).fetchall()
    return [row["run_id"] for row in rows]


def get_risk_results(conn: sqlite3.Connection, run_id: str) -> pd.DataFrame:
    """지정 run의 위험 판정 결과 전체."""
    query = "SELECT * FROM risk_results WHERE run_id = :run_id ORDER BY item_id"
    return pd.read_sql_query(query, conn, params={"run_id": run_id})


def get_forecast(conn: sqlite3.Connection, run_id: str, item_id: str) -> dict | None:
    """지정 run·품목의 예측 1행을 dict로(daily_json은 json.loads 해 daily 리스트로 변환).

    없으면 None.
    """
    row = conn.execute(
        "SELECT * FROM forecasts WHERE run_id = ? AND item_id = ?",
        (run_id, item_id),
    ).fetchone()
    if row is None:
        return None

    result = dict(row)
    result["daily"] = json.loads(result.pop("daily_json"))
    return result


def list_action_history(
    conn: sqlite3.Connection,
    *,
    item_id: str | None = None,
    ingredient_code: str | None = None,
    risk_type: str | None = None,
    limit: int | None = None,
) -> pd.DataFrame:
    """조치 이력 최신순 목록(품목명 포함).

    ingredient_code 필터는 items 조인을 경유한다. risk_type은 action_history.risk_type
    컬럼과 정확히 일치하는 행만 남긴다(해당 컬럼은 M-21 이력 참조용으로 이후 추가되어,
    v1 당시 "미지원" 제약은 해소되었다 — task-M18-brief.md).
    """
    query = """
        SELECT h.*, i.item_name
        FROM action_history AS h
        JOIN items AS i ON i.item_id = h.item_id
        WHERE 1 = 1
    """
    params: dict[str, object] = {}

    if item_id is not None:
        query += " AND h.item_id = :item_id"
        params["item_id"] = item_id
    if ingredient_code is not None:
        query += " AND i.ingredient_code = :ingredient_code"
        params["ingredient_code"] = ingredient_code
    if risk_type is not None:
        query += " AND h.risk_type = :risk_type"
        params["risk_type"] = risk_type

    query += " ORDER BY h.created_at DESC, h.history_id DESC"

    if limit is not None:
        query += " LIMIT :limit"
        params["limit"] = limit

    return pd.read_sql_query(query, conn, params=params)


def fetch_alerts(
    conn: sqlite3.Connection, *, unread_only: bool = False, limit: int = 50
) -> pd.DataFrame:
    """알림 최신순 목록. unread_only=True면 미확인(is_read=0) 알림만."""
    query = "SELECT * FROM alerts WHERE 1 = 1"
    params: dict[str, object] = {"limit": limit}

    if unread_only:
        query += " AND is_read = 0"

    query += " ORDER BY created_at DESC, alert_id DESC LIMIT :limit"
    return pd.read_sql_query(query, conn, params=params)


def get_meta(conn: sqlite3.Connection) -> dict:
    """meta 테이블 전체를 dict로."""
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {row["key"]: row["value"] for row in rows}
