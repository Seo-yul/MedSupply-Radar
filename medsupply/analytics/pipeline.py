"""분석 평가 파이프라인 — 순수 함수(forecast·anomaly·depletion·risk)를 배치·화면·측정이
공유하는 단일 평가 경로로 결선(wiring)한다.

**이 모듈만 예외적으로 DB 어댑터 코드를 가진다.** assess_snapshot이 sqlite3.Connection을
받아 medsupply.data.queries 경유로 읽기 전용 로드를 수행하고, stock_usage_daily 전체를
1회 SELECT하는 내부 헬퍼(_load_all_usage)를 갖는다. 이 모듈 자체는 위험 판정 계산 로직을
전혀 갖지 않는다 — 모든 계산은 기존 순수 함수(medsupply.analytics.{forecast,anomaly,
depletion,risk})에 그대로 위임하고, 이 모듈은 다음 세 층의 결선만 담당한다:

- build_item_inputs: DataFrame(items·usage·receipts·notice_map) → list[ItemInputs]
- assess_item/assess_all: ItemInputs → RiskAssessment (순수 함수 호출 순서 결선)
- assess_snapshot: sqlite3.Connection → pd.DataFrame (조회 오케스트레이션 + 위 두 층 호출)
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date

import pandas as pd

from medsupply.analytics.anomaly import detect_receipt_delay, detect_usage_anomalies
from medsupply.analytics.depletion import estimate_depletion
from medsupply.analytics.forecast import sma_forecast, ses_forecast
from medsupply.analytics.params import AnalyticsParams, load_params
from medsupply.analytics.risk import compute_score, derive_risk_type, grade_risk
from medsupply.analytics.types import ForecastResult, ItemInputs, RiskAssessment
from medsupply.data import queries

_SNAPSHOT_COLUMNS = (
    "item_id",
    "grade",
    "base_grade",
    "escalated_by_notice",
    "risk_type",
    "score",
    "days_to_stockout",
    "depletion_date",
    "factors_json",
    "horizon_days",
    "avg_daily_forecast",
    "total_forecast",
    "daily_json",
)


def build_item_inputs(
    items_df: pd.DataFrame,
    usage_df: pd.DataFrame,
    receipts_df: pd.DataFrame,
    notice_map_df: pd.DataFrame,
    as_of: date,
) -> list[ItemInputs]:
    """품목·시계열·입고·공고 매핑 DataFrame들을 ItemInputs 목록으로 결선한다.

    Args:
        items_df: 최소 컬럼 item_id, is_essential(평가 대상 품목 전체 목록).
        usage_df: 최소 컬럼 item_id, date(TEXT ISO), usage_qty, closing_stock.
            일자 오름차순이 보장되지 않으므로(호출부 책임 아님) 이 함수가 품목별로 정렬한다.
        receipts_df: incoming_shipments 컬럼(item_id 포함) 부분집합. 날짜 컬럼은 TEXT
            그대로 전달한다 — 이 함수는 날짜 파싱을 하지 않으며, 하위 순수 함수
            (anomaly.detect_receipt_delay·depletion.estimate_depletion)가 파싱한다.
        notice_map_df: get_active_notice_map(as_of) 반환 형태(notice_id, item_id,
            needs_review 포함) — **이미 "활성" 매핑만 담긴 것을 전제한다.**
            needs_review==1 매핑을 상향 판단에서 제외할지(params.grade.escalate_needs_review)는
            params가 필요한 판단이지만, 이 함수의 시그니처는 params를 받지 않는다(계약 고정).
            그래서 그 판단은 **상위 호출자(assess_snapshot)가 notice_map_df를 사전 필터해서
            넘기는 방식**으로 구현한다 — 이 함수는 넘어온 notice_map_df에 대해 단순히
            "해당 item_id의 행이 존재하는가"만 판정한다(needs_review 값 자체는 보지 않는다).
        as_of: 평가 기준일.

    Returns:
        item_id 오름차순으로 정렬된 ItemInputs 목록(결정성 보장).

    Notes:
        - **날짜 결선**: usage_df.date(TEXT)는 ``datetime.date`` 인덱스로 변환해
          ItemInputs.usage(pd.Series)에 담는다 — ``.dt.date``로 파이썬 date 객체를
          얻으며, pd.Timestamp는 인덱스에 쓰지 않는다.
        - **룩어헤드 가드**: usage는 date <= as_of인 행만 포함한다. as_of를 초과하는
          행은 조용히 무시되는 게 아니라 — 이 docstring에 명시적으로 문서화된 대로 —
          결과에서 **잘라낸다**(백테스트 시 미래 데이터가 새어 들어가는 것을 막기 위한
          안전장치).
        - stock_on_hand: as_of 이하 마지막(최신) closing_stock. 해당 품목이 usage_df에
          전혀 없으면 0.0.
        - has_active_notice는 ``bool(...)``로 엄격 변환한다(numpy.bool_ 등 금지).
    """
    usage_df = usage_df.copy()
    usage_df["date"] = pd.to_datetime(usage_df["date"]).dt.date
    usage_df = usage_df[usage_df["date"] <= as_of]
    usage_df = usage_df.sort_values(["item_id", "date"])

    active_notice_item_ids = set(notice_map_df["item_id"])

    results: list[ItemInputs] = []
    for _, item_row in items_df.iterrows():
        item_id = item_row["item_id"]
        item_usage = usage_df[usage_df["item_id"] == item_id]

        if len(item_usage) > 0:
            usage_series = pd.Series(
                item_usage["usage_qty"].astype(float).to_numpy(),
                index=item_usage["date"].to_numpy(),
            )
            stock_on_hand = float(item_usage["closing_stock"].iloc[-1])
        else:
            usage_series = pd.Series([], dtype=float)
            stock_on_hand = 0.0

        item_receipts = receipts_df[receipts_df["item_id"] == item_id]
        has_active_notice = bool(item_id in active_notice_item_ids)

        results.append(
            ItemInputs(
                item_id=item_id,
                as_of=as_of,
                stock_on_hand=stock_on_hand,
                usage=usage_series,
                receipts=item_receipts,
                has_active_notice=has_active_notice,
                is_essential=bool(item_row["is_essential"]),
            )
        )

    results.sort(key=lambda inputs: inputs.item_id)
    return results


def assess_item(inputs: ItemInputs, params: AnalyticsParams) -> RiskAssessment:
    """단일 품목 ItemInputs를 순수 함수들로 결선해 RiskAssessment를 산출한다.

    결선 순서: forecast(sma|ses, params.forecast.method에 따라) → anomalies(usage +
    receipt_delay) → depletion(estimate_depletion) → grade(grade_risk) →
    risk_type(derive_risk_type) → score(compute_score). 이 함수는 계산 로직을 전혀
    갖지 않는다 — 각 단계는 기존 순수 함수(medsupply.analytics.{forecast,anomaly,
    depletion,risk})에 그대로 위임한다.

    Notes:
        - **빈 usage 품목(시계열 없음)**: inputs.usage가 비어 있으면 sma_forecast/
          ses_forecast는 ValueError를 던진다(순수 함수 자신의 계약). 이 파이프라인은
          "전 품목 평가 원칙"에 따라 예외를 전파하지 않는다 — method='none' 같은
          types 계약 밖의 값을 쓰는 대신, ForecastResult 타입 계약은 그대로 유지한
          채 avg_daily=0.0, daily=(0.0,) * params.forecast.horizon_days, total=0.0,
          method=params.forecast.method(설정된 방법명 그대로)로 "0 예측"을 구성해
          이후 단계(depletion 이하)를 동일한 경로로 계속 진행한다. 이 0 예측이
          depletion.estimate_depletion에 들어가면 avg_daily==0 규칙에 따라 자연히
          days_to_stockout=None이 된다(재고가 이미 0/음수인 경우는 estimate_depletion의
          일반 규칙 — 즉시 소진 — 을 그대로 따른다. 특별 취급하지 않는다).
        - anomalies는 usage 이상탐지 결과가 먼저, 입고 지연 이상탐지 결과가 그 다음인
          tuple이다(``tuple(detect_usage_anomalies(...)) + tuple(detect_receipt_delay(...))``).
          입고 지연 탐지는 usage 시계열과 무관하게 receipts만 보므로, usage가 비어
          있어도 정상적으로(건너뛰지 않고) 수행된다.
        - RiskAssessment.reflected_receipts는 depletion.reflected_receipts를 그대로
          옮긴 값이다(승인된 계약 변경).
    """
    if len(inputs.usage) == 0:
        forecast = ForecastResult(
            method=params.forecast.method,
            horizon_days=params.forecast.horizon_days,
            daily=(0.0,) * params.forecast.horizon_days,
            avg_daily=0.0,
            total=0.0,
        )
    elif params.forecast.method == "sma":
        forecast = sma_forecast(inputs.usage, params.forecast.sma_window, params.forecast.horizon_days)
    else:
        forecast = ses_forecast(inputs.usage, params.forecast.ses_alpha, params.forecast.horizon_days)

    usage_anomalies = detect_usage_anomalies(inputs.usage, inputs.as_of, params.anomaly)
    receipt_anomalies = detect_receipt_delay(inputs.receipts, inputs.as_of, params.anomaly)
    anomalies = tuple(usage_anomalies) + tuple(receipt_anomalies)

    depletion = estimate_depletion(
        inputs.stock_on_hand, forecast.daily, inputs.receipts, inputs.as_of, params.depletion
    )

    decision = grade_risk(depletion.days_to_stockout, inputs.has_active_notice, params.grade)
    risk_type = derive_risk_type(anomalies, inputs.has_active_notice)
    score = compute_score(decision, anomalies, inputs.has_active_notice, params.score)

    return RiskAssessment(
        item_id=inputs.item_id,
        as_of=inputs.as_of,
        grade=decision.grade,
        base_grade=decision.base_grade,
        escalated_by_notice=decision.escalated_by_notice,
        risk_type=risk_type,
        score=score,
        days_to_stockout=depletion.days_to_stockout,
        depletion_date=depletion.depletion_date,
        forecast=forecast,
        anomalies=anomalies,
        reflected_receipts=depletion.reflected_receipts,
    )


def assess_all(items: Sequence[ItemInputs], params: AnalyticsParams) -> list[RiskAssessment]:
    """item_id 오름차순으로 평가해 반환한다(입력 순서 무관 — 출력 결정성 보장)."""
    ordered_items = sorted(items, key=lambda inputs: inputs.item_id)
    return [assess_item(inputs, params) for inputs in ordered_items]


def _load_all_usage(conn: sqlite3.Connection) -> pd.DataFrame:
    """stock_usage_daily 전체를 1회 SELECT로 읽는다(품목별 반복 조회 회피).

    이 모듈에서만 허용되는 예외적 직접 SELECT다(읽기 전용 — INSERT/UPDATE/DELETE 없음).
    반환 columns: item_id, date, usage_qty, closing_stock.
    """
    query = "SELECT item_id, date, usage_qty, closing_stock FROM stock_usage_daily ORDER BY item_id, date"
    return pd.read_sql_query(query, conn)


def assess_snapshot(
    conn: sqlite3.Connection, as_of: date, params: AnalyticsParams | None = None
) -> pd.DataFrame:
    """conn이 가리키는 스냅샷의 전 품목을 평가해 DataFrame으로 반환한다.

    배치·화면·측정·재현성 검증이 공유하는 평가 파이프라인의 진입점이다. 이 함수(와
    모듈 내부 헬퍼 _load_all_usage)만이 DB를 읽는다 — 모두 medsupply.data.queries
    경유이거나 명시적으로 읽기 전용인 SELECT다. run_id 생성과 결과 영속화(writer 호출)는
    이 함수의 소관이 아니다(S-15 배치 실행기가 이 함수의 반환 DataFrame을 받아
    writer.save_risk_results·save_forecasts에 나누어 저장한다).

    Args:
        conn: 읽기 전용으로만 사용하는 sqlite3.Connection.
        as_of: 평가 기준일.
        params: None이면 load_params()로 config/analytics_params.toml 기본값을 로드한다.

    Returns:
        컬럼: item_id, grade(str, RiskGrade.value), base_grade(str), escalated_by_notice
        (int 0/1), risk_type, score, days_to_stockout(Int64, None 허용),
        depletion_date(str|None, ISO), factors_json(str —
        json.dumps(RiskAssessment.to_evidence(), ensure_ascii=False)), horizon_days,
        avg_daily_forecast, total_forecast, daily_json(str — json.dumps(list(daily))).
        writer.save_risk_results·save_forecasts가 요구하는 컬럼을 모두 포함하도록 맞춘
        구성이다(다음 태스크 S-15가 이 DataFrame을 두 writer 입력으로 분리한다).

    Notes:
        items는 list_items(conn)의 전 품목(run_id 조인 결과는 무시 — item_id·is_essential만
        사용), usage는 stock_usage_daily 전체를 1회 SELECT(_load_all_usage)한 뒤 품목별로
        나눈다(품목별 get_daily_series 반복 호출은 느려서 피한다), receipts는
        get_incoming_shipments(conn, pending_only=False)로 전체를 로드한다(pending 여부
        필터링은 하위 순수 함수가 담당). notice_map은 get_active_notice_map(conn, as_of)로
        "활성" 매핑만 로드한 뒤, params.grade.escalate_needs_review=False면 needs_review==1
        매핑을 여기서 제외하고 build_item_inputs에 넘긴다(build_item_inputs는 params를
        받지 않으므로 이 필터링은 반드시 이 함수의 책임이다).
    """
    if params is None:
        params = load_params()

    items_df = queries.list_items(conn)
    usage_df = _load_all_usage(conn)
    receipts_df = queries.get_incoming_shipments(conn, pending_only=False)
    notice_map_df = queries.get_active_notice_map(conn, as_of)

    if not params.grade.escalate_needs_review and len(notice_map_df) > 0:
        notice_map_df = notice_map_df[notice_map_df["needs_review"] != 1]

    items = build_item_inputs(items_df, usage_df, receipts_df, notice_map_df, as_of)
    assessments = assess_all(items, params)

    records = [
        {
            "item_id": a.item_id,
            "grade": a.grade.value,
            "base_grade": a.base_grade.value,
            "escalated_by_notice": int(a.escalated_by_notice),
            "risk_type": a.risk_type,
            "score": a.score,
            "days_to_stockout": a.days_to_stockout,
            "depletion_date": a.depletion_date.isoformat() if a.depletion_date is not None else None,
            "factors_json": json.dumps(a.to_evidence(), ensure_ascii=False),
            "horizon_days": a.forecast.horizon_days,
            "avg_daily_forecast": a.forecast.avg_daily,
            "total_forecast": a.forecast.total,
            "daily_json": json.dumps(list(a.forecast.daily)),
        }
        for a in assessments
    ]

    df = pd.DataFrame(records, columns=list(_SNAPSHOT_COLUMNS))
    df["days_to_stockout"] = df["days_to_stockout"].astype("Int64")
    return df
