"""Pure functions for risk grade, risk type, and score decisions.

품절 위험도 산정의 핵심 결정 로직. **LLM 미관여 순수 함수** — 동일 입력에 항상
동일 판정을 반환한다(I/O·datetime.now()·난수 금지). 이 모듈의 출력이
risk_results 테이블·화면 등급·감지율 측정의 원천이다.
"""

from __future__ import annotations

from collections.abc import Sequence

from medsupply.analytics.params import GradeParams, ScoreParams
from medsupply.analytics.types import GRADE_ORDER, AnomalyFlag, GradeDecision, RiskGrade


def grade_risk(days_to_stockout: int | None, has_active_notice: bool, params: GradeParams) -> GradeDecision:
    """소진 예상 일수와 활성 공고 여부로 위험 등급을 산정한다.

    등급 규칙:
        - base 등급: days_to_stockout가 None이면 정상. 아니면
          <= danger_days → 위험, <= warning_days → 경고,
          <= watch_days → 주의, 그 외 → 정상.
          (0·음수도 danger_days 이하이므로 위험으로 분류된다.)
        - 상향: has_active_notice and params.escalate_on_notice이면 base 등급에서
          GRADE_ORDER 상 한 단계 상향한다(정상→주의→경고→위험). 위험 등급은
          더 상향되지 않는다(캡) — 이 경우 grade == base_grade이므로
          escalated_by_notice는 False가 된다.
          escalate_on_notice=False이면 공고가 있어도 상향하지 않는다.

    Note:
        '확인 필요' 상태의 공고를 상향 대상으로 볼지(escalate_needs_review)는
        이 함수의 책임이 아니다. has_active_notice는 이미 계산된 값을 그대로
        받으며, 그 값을 어떻게 산출할지(공고 status를 어디까지 '활성'으로
        볼지)는 이 함수를 호출하는 상위 파이프라인이 params.escalate_needs_review를
        참고해 결정한다.

    Args:
        days_to_stockout: 예상 소진까지 남은 일수. None이면 소진 예상 없음.
        has_active_notice: 활성 공급 공고 존재 여부(상위 파이프라인이 계산).
        params: GradeParams(danger_days, warning_days, watch_days,
            escalate_on_notice, escalate_needs_review).

    Returns:
        GradeDecision(grade=최종 등급, base_grade=상향 전 등급,
        escalated_by_notice=(최종 != base)).
    """
    if days_to_stockout is None:
        base_grade = RiskGrade.NORMAL
    elif days_to_stockout <= params.danger_days:
        base_grade = RiskGrade.DANGER
    elif days_to_stockout <= params.warning_days:
        base_grade = RiskGrade.WARNING
    elif days_to_stockout <= params.watch_days:
        base_grade = RiskGrade.WATCH
    else:
        base_grade = RiskGrade.NORMAL

    grade = base_grade
    if has_active_notice and params.escalate_on_notice:
        current_index = GRADE_ORDER.index(base_grade)
        escalated_index = max(0, current_index - 1)
        grade = GRADE_ORDER[escalated_index]

    return GradeDecision(
        grade=grade,
        base_grade=base_grade,
        escalated_by_notice=(grade != base_grade),
    )


def derive_risk_type(anomalies: Sequence[AnomalyFlag], has_active_notice: bool) -> str:
    """위험 요인 조합으로 risk_type을 유도한다(마스터 플랜 결정 27).

    요인 집합: 활성 공고(has_active_notice), 수요 급증(anomalies에
    kind=='usage_surge' 존재), 입고 지연(kind=='receipt_delay' 존재).
    usage_drop은 요인에 포함하지 않는다 — 품절 위험 유형에 기여하지 않으므로
    무시한다.

    - 요인 2개 이상 → 'composite'
    - 정확히 1개 → 'supply_halt'(공고) / 'demand_surge'(급증) / 'delivery_delay'(지연)
    - 0개 → 'general'

    Args:
        anomalies: 감지된 이상 신호 시퀀스.
        has_active_notice: 활성 공급 공고 존재 여부.

    Returns:
        risk_results.risk_type CHECK 제약 집합의 원소:
        'demand_surge' | 'supply_halt' | 'delivery_delay' | 'composite' | 'general'.
    """
    has_surge = any(a.kind == "usage_surge" for a in anomalies)
    has_delay = any(a.kind == "receipt_delay" for a in anomalies)

    factor_count = sum((has_active_notice, has_surge, has_delay))

    if factor_count >= 2:
        return "composite"
    if factor_count == 0:
        return "general"

    if has_active_notice:
        return "supply_halt"
    if has_surge:
        return "demand_surge"
    return "delivery_delay"


def compute_score(
    decision: GradeDecision,
    anomalies: Sequence[AnomalyFlag],
    has_active_notice: bool,
    params: ScoreParams,
) -> int:
    """최종 등급·이상 신호·활성 공고로 위험 점수(0~100)를 산정한다.

    base = decision.grade(상향 반영된 최종 등급) 기준 기본점
    (base_danger/base_warning/base_watch/base_normal). base_grade가 아닌
    최종 grade를 기준으로 한다.

    가점 = params.per_anomaly * len(anomalies) + (notice_bonus if has_active_notice else 0).

    반환 = min(100, base + 가점).

    Note:
        anomalies 개수는 usage_drop을 포함한 전체 개수를 센다 — 신호 밀도를
        반영하기 위함이며, derive_risk_type이 usage_drop을 요인에서 제외하는
        것과는 다르다.

    Args:
        decision: grade_risk의 반환값(최종 grade 필드를 사용).
        anomalies: 감지된 이상 신호 시퀀스(전체, 종류 무관하게 개수만 사용).
        has_active_notice: 활성 공급 공고 존재 여부.
        params: ScoreParams(base_danger, base_warning, base_watch, base_normal,
            per_anomaly, notice_bonus).

    Returns:
        0~100 범위의 정수 위험 점수.
    """
    base_by_grade = {
        RiskGrade.DANGER: params.base_danger,
        RiskGrade.WARNING: params.base_warning,
        RiskGrade.WATCH: params.base_watch,
        RiskGrade.NORMAL: params.base_normal,
    }
    base = base_by_grade[decision.grade]
    bonus = params.per_anomaly * len(anomalies) + (params.notice_bonus if has_active_notice else 0)

    return min(100, base + bonus)
