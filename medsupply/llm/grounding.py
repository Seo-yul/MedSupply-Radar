"""위험 근거 패키징(M-20) — collect_risk_evidence + 환각 사후 대조기.

이 모듈에 LLM 호출은 없다(전부 결정적 코드). collect_risk_evidence는 risk_results
최신(또는 지정) run 1건을 재산출 없이 조회해 원인 설명 생성(M-21)이 볼 수 있는
사실의 전체 집합(RiskEvidence, closed-world)을 조립한다 — 판정(등급·점수)과 생성
(설명·대응방안)을 분리하는 마스터 플랜 원칙을 그대로 따른다. verify_explanation_grounding은
그 생성물이 근거 밖 사실(ID·수치·날짜·공고 존재)을 말했는지 결정적으로 대조해
hallucination_flags(list[str])를 반환한다 — 위반이어도 예외를 던지지 않는다(M-21이
결과에 그대로 부착한다).

새 SQL은 여기서 직접 작성하지 않는다 — 전부 medsupply.data.queries 함수 조합이다(계층
규칙, task-M16-brief.md). medsupply.services.workbench와 조립 로직이 일부 겹치지만
그 모듈은 UI 캐시 계층(st.cache_data)이라 재사용하지 않고 queries를 직접 다시 조합한다
(task-M20-brief.md 지시).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import date, timedelta

import pandas as pd

from medsupply.analytics import asof
from medsupply.data import queries
from medsupply.llm.schemas import RiskEvidence, RiskExplanation

#: 사용량 평균 산출 윈도우(일) — services/workbench.py _usage_averages와 동일(F2: 화면·근거
#: 수치 일치를 위해 반올림 순서까지 맞춘다 — 각 창을 round 1 한 뒤에 변화율을 계산한다).
_USAGE_WINDOW_DAYS = 28
_SERIES_WINDOW_DAYS = _USAGE_WINDOW_DAYS * 2

#: ISO 날짜(YYYY-MM-DD) 패턴 — unsupported_date 검사의 후보이자, unsupported_number 검사에서
#: "날짜 구성부"를 먼저 걷어내는 마스킹 대상이다.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
#: "2026년"처럼 ISO 형식이 아닌 단독 연도 표기 — unsupported_number 검사에서 함께 마스킹한다
#: (브리프: "2자리 이상 숫자(연도·날짜 구성부 제외)"). 4자리라는 이유만으로 전부 면제하지는
#: 않는다 — "년" 뒤에 붙은 경우만 연도로 본다(순수 4자리 수량은 정상적으로 대조 대상이다).
_YEAR_RE = re.compile(r"\d{4}(?=년)")
#: 수치 토큰 — 부호는 토큰화하지 않는다(픽스 라운드 1 리뷰 F5). "10-20개" 같은 범위 표기의
#: 하이픈을 음수 부호로 오인하면 "-20"이 돼버려 실제로는 두 양수 10·20을 검사해야 할 자리에
#: 엉뚱한 음수 하나만 남는다. 음수로 존재하는 evidence 값(예: usage_change_pct)과의 대조는
#: 토큰화가 아니라 _numeric_equivalents의 절대값 허용이 담당한다.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
#: 숫자 문자만 남겨 자릿수를 세기 위한 보조 패턴("2자리 이상" 판정에 소수점은 제외).
_DIGITS_ONLY_RE = re.compile(r"[^\d]")


def _unique_preserve_order(items) -> list[str]:
    """items를 최초 등장 순서를 지킨 채 중복 제거한다.

    collect_risk_evidence의 evidence_refs(리뷰 F1: 집합이어야 함)와
    verify_explanation_grounding의 플래그 순서 결정성 둘 다가 이 헬퍼를 공유한다.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _resolve_run_id(conn: sqlite3.Connection, run_id: str | None) -> str | None:
    """run_id가 None이면 get_latest_runs(1)의 최신 run으로 해석한다(run이 전혀 없으면 None)."""
    if run_id is not None:
        return run_id
    latest = queries.get_latest_runs(conn, 1)
    return latest[0] if latest else None


def _usage_stats(series: pd.DataFrame) -> tuple[float | None, float | None, float | None]:
    """(current_stock, avg_daily_usage, usage_change_pct).

    series가 비어 있으면 셋 다 None. avg_daily_usage는 series에 있는 만큼(최대 28일)의
    usage_qty 평균(소수 1자리)이며 1행만 있어도 계산된다. usage_change_pct는 "그 직전
    28일"이 온전히 존재할 때만(len(series) >= 56) 계산한다 — 두 창을 각각 round 1 한 뒤
    변화율을 구해(services/workbench.py _usage_averages와 동일 순서, F2) 화면 수치와
    근거 수치가 어긋나지 않게 한다. 직전 28일 평균이 0이면(변화율 정의 불가) None으로 둔다.
    """
    if series.empty:
        return None, None, None

    last_stock = series.iloc[-1]["closing_stock"]
    current_stock = None if pd.isna(last_stock) else float(last_stock)

    recent = series.tail(_USAGE_WINDOW_DAYS)
    avg_daily_usage = round(float(recent["usage_qty"].mean()), 1)

    if len(series) < _SERIES_WINDOW_DAYS:
        return current_stock, avg_daily_usage, None

    prev_window = series.iloc[-_SERIES_WINDOW_DAYS : -_USAGE_WINDOW_DAYS]
    avg_prev = round(float(prev_window["usage_qty"].mean()), 1)
    if avg_prev == 0:
        return current_stock, avg_daily_usage, None

    usage_change_pct = round(((avg_daily_usage - avg_prev) / avg_prev) * 100, 1)
    return current_stock, avg_daily_usage, usage_change_pct


def _active_notices(conn: sqlite3.Connection, item_id: str, as_of_date: date) -> list[dict]:
    """as_of 시점 활성 공고 중 item_id에 매핑된 것만, published_date 내림차순·notice_id
    오름차순으로 정렬해 반환한다.

    get_active_notice_map은 notice_id 오름차순으로만 정렬돼 있어(활성 판정의 단일 소스),
    published_date 기준 정렬은 여기서 별도로 다시 한다 — 안정 정렬 2단계(먼저 notice_id
    오름차순, 그 다음 published_date 내림차순)로 동일 날짜 tie-break를 notice_id 오름차순으로
    고정한다.
    """
    active_map = queries.get_active_notice_map(conn, as_of=as_of_date)
    if active_map.empty:
        return []

    notice_ids = list(active_map.loc[active_map["item_id"] == item_id, "notice_id"])
    notices: list[dict] = []
    for notice_id in notice_ids:
        detail = queries.get_notice_detail(conn, notice_id)
        if detail is None:  # pragma: no cover - 방어적(활성 맵이 가리키는 공고는 항상 존재)
            continue
        payload = detail.get("payload") or {}
        notices.append(
            {
                "notice_id": notice_id,
                "title": detail["title"],
                "notice_type": detail["notice_type"],
                "published_date": detail["published_date"],
                "reason": payload.get("reason"),
            }
        )

    notices.sort(key=lambda n: n["notice_id"])
    notices.sort(key=lambda n: n["published_date"], reverse=True)
    return notices


def _next_shipment(
    conn: sqlite3.Connection, item_id: str, as_of_date: date
) -> tuple[dict | None, int | None]:
    """(next_shipment dict|None, shipment_id|None) — as_of 시점 미래 예정(pending) 최근접 1건.

    actual_date IS NULL만으로 "미입고"를 판정하면 연체 건(예정일이 이미 지났는데 미입고)까지
    "다음 입고"로 잘못 선정한다(2주차 브랜치 리뷰 F2) — pending_only=False로 전체를 가져와
    asof.is_pending_at으로 as_of 시점 상태를 재구성한다. 반환 dict는 {expected_date, qty}만
    담고(스키마 계약), shipment_id는 evidence_refs의 shipment:{shipment_id} 채번에만 쓰도록
    별도로 반환한다.
    """
    shipments = queries.get_incoming_shipments(conn, item_id=item_id, pending_only=False)
    if shipments.empty:
        return None, None

    parsed = shipments.copy()
    for col in ("expected_date", "actual_date"):
        parsed[col] = pd.to_datetime(parsed[col], errors="coerce").dt.date

    pending_mask = pd.Series(
        [
            asof.is_pending_at(expected, actual, as_of_date)
            for expected, actual in zip(parsed["expected_date"], parsed["actual_date"])
        ],
        index=parsed.index,
    )
    pending = shipments[pending_mask]
    if pending.empty:
        return None, None

    row = pending.iloc[0]
    qty = row["expected_qty"]
    next_shipment = {
        "expected_date": row["expected_date"],
        "qty": None if pd.isna(qty) else int(qty),
    }
    return next_shipment, int(row["shipment_id"])


def _substitutes_same_condition(
    conn: sqlite3.Connection, item_id: str, as_of_date: date
) -> list[dict]:
    """같은 대체군(same_condition_only=True) 품목 — item_id 오름차순(쿼리 정렬 그대로).

    as_of_date를 queries.get_substitutes에 그대로 전달해, run의 as_of 이후에 기록된
    재고(예: 과거 run 조회 시 그 뒤에 쌓인 최신 데이터)가 "현재 재고"로 끌려 들어오는
    룩어헤드를 막는다(리뷰 F4).
    """
    df = queries.get_substitutes(conn, item_id, same_condition_only=True, as_of=as_of_date)
    return [
        {
            "item_id": row["item_id"],
            "item_name": row["item_name"],
            "supplier": row["supplier"],
            "current_stock": None if pd.isna(row["current_stock"]) else int(row["current_stock"]),
        }
        for _, row in df.iterrows()
    ]


def collect_risk_evidence(
    conn: sqlite3.Connection, item_id: str, run_id: str | None = None
) -> RiskEvidence:
    """LLM 원인 설명(M-21)이 볼 수 있는 사실의 전체 집합을 결정적으로 조립한다.

    위험 파트는 risk_results를 재산출하지 않고 그대로 조회한다(판정·생성 분리) — run_id가
    None이면 get_latest_runs(1)의 최신 run을 쓴다. 지정/해석된 run에 item_id 행이 없으면
    ValueError(메시지에 item_id·run_id 포함)를 던진다.

    as_of는 그 run의 risk_results.as_of를 그대로 쓴다(호출 시점의 meta.base_date가 아니다) —
    run_id로 과거 run을 지정해도 그 시점 기준으로 active_notices·next_shipment·사용량 창이
    일관되게 재구성되도록 하기 위함이다(동일 DB·인자 → 동일 결과).

    모든 값은 queries.py 함수 조회로만 채운다(원시 SQL 없음). evidence_refs 채번 규칙은
    task-M20-brief.md 표를 그대로 따른다: risk:{run_id}, usage:recent28, stock:current(항상
    포함), anomaly:{seq}:{kind}(anomalies 각각, seq=1-based 리스트 위치 — detected_on은
    as_of와 동일해 충돌하는 경우가 있어 판별자로 못 쓴다, 리뷰 F1), notice:{notice_id}
    (active_notices 각각), shipment:{shipment_id}(next_shipment 있을 때만), substitute:
    {item_id}(substitutes_same_condition 각각). evidence_refs는 집합이다 — 마지막에
    _unique_preserve_order로 중복을 제거해 반환한다(리뷰 F1).
    """
    resolved_run_id = _resolve_run_id(conn, run_id)

    risk_df = queries.get_risk_results(conn, resolved_run_id)
    match = risk_df[risk_df["item_id"] == item_id]
    if match.empty:
        raise ValueError(
            f"collect_risk_evidence: no risk_results row for item_id={item_id!r},"
            f" run_id={resolved_run_id!r}"
        )
    risk_row = match.iloc[0].to_dict()

    item = queries.get_item(conn, item_id)
    as_of_value = risk_row["as_of"]
    as_of_date = date.fromisoformat(as_of_value)

    factors_raw = risk_row.get("factors_json")
    factors = json.loads(factors_raw) if factors_raw else {}
    anomalies = factors.get("anomalies", [])

    series = queries.get_daily_series(
        conn,
        item_id,
        start=as_of_date - timedelta(days=_SERIES_WINDOW_DAYS - 1),
        end=as_of_date,
    )
    current_stock, avg_daily_usage, usage_change_pct = _usage_stats(series)

    active_notices = _active_notices(conn, item_id, as_of_date)
    next_shipment, shipment_id = _next_shipment(conn, item_id, as_of_date)
    substitutes = _substitutes_same_condition(conn, item_id, as_of_date)

    evidence_refs = [f"risk:{resolved_run_id}", "usage:recent28", "stock:current"]
    evidence_refs += [f"anomaly:{seq}:{a['kind']}" for seq, a in enumerate(anomalies, start=1)]
    evidence_refs += [f"notice:{n['notice_id']}" for n in active_notices]
    if next_shipment is not None:
        evidence_refs.append(f"shipment:{shipment_id}")
    evidence_refs += [f"substitute:{s['item_id']}" for s in substitutes]
    evidence_refs = _unique_preserve_order(evidence_refs)

    return RiskEvidence(
        item_id=item_id,
        item_name=item["item_name"],
        ingredient_name_kr=item.get("ingredient_name_kr"),
        as_of=as_of_value,
        run_id=resolved_run_id,
        grade=risk_row["grade"],
        score=int(risk_row["score"]),
        risk_type=risk_row["risk_type"],
        days_to_stockout=(
            None if pd.isna(risk_row.get("days_to_stockout")) else int(risk_row["days_to_stockout"])
        ),
        depletion_date=risk_row.get("depletion_date"),
        current_stock=current_stock,
        avg_daily_usage=avg_daily_usage,
        usage_change_pct=usage_change_pct,
        anomalies=anomalies,
        escalated_by_notice=bool(risk_row.get("escalated_by_notice")),
        active_notices=active_notices,
        next_shipment=next_shipment,
        substitutes_same_condition=substitutes,
        evidence_refs=evidence_refs,
    )


# ---------------------------------------------------------------------------
# verify_explanation_grounding — 결정적 환각 사후 대조기
# ---------------------------------------------------------------------------


def _body_text(explanation: RiskExplanation) -> str:
    """대조 대상 본문 = cause_summary + 각 action.description(브리프 §unsupported_date 정의를
    unsupported_number·phantom_notice에도 동일 적용)."""
    parts = [explanation.cause_summary] + [action.description for action in explanation.actions]
    return "\n".join(parts)


def _dates_in_text(text: str) -> list[str]:
    """text에 등장하는 ISO 날짜(YYYY-MM-DD)를 등장 순서 그대로(중복 포함) 반환한다.

    본문 스캔(순서가 플래그 순서를 결정)과 evidence 풀 파생(순서 무관, 집합으로 합침)
    양쪽이 공유하는 단일 추출 지점이다.
    """
    return _ISO_DATE_RE.findall(text)


def _number_tokens_in_text(text: str) -> list[str]:
    """text에서 "2자리 이상 숫자" 토큰만(날짜 구성부 제외) 최초 등장 순서로 추출한다.

    ISO 날짜 전체와 "NNNN년"(단독 연도 표기)을 먼저 공백으로 치환해 걷어낸 뒤 숫자를
    찾는다 — 빈 문자열이 아니라 공백으로 치환해 그 결과 인접 숫자가 우연히 이어붙지
    않게 한다. 본문 스캔과 evidence 풀 파생(리뷰 F2) 양쪽이 공유하는 단일 추출 지점이라,
    부호 없는 토큰화(리뷰 F5)도 이 한 곳만 고치면 양쪽에 동시 반영된다.
    """
    masked = _YEAR_RE.sub(" ", _ISO_DATE_RE.sub(" ", text))
    return [
        token
        for token in _NUMBER_RE.findall(masked)
        if len(_DIGITS_ONLY_RE.sub("", token)) >= 2
    ]


def _numeric_equivalents(value: float) -> set[float]:
    """value의 "허용 표현" 집합 — 절대값과 원부호 각각에 대해 소수 1자리 그대로, 그리고
    정수화 시 내림(floor)·올림(ceil) 양방향을 전부 더한다.

    리뷰 F3: 0.5 경계에서 "half-up"과 "floor"(또는 그 반대) 어느 쪽으로 인용해도 허용해야
    한다는 룰링에 따라, 단일 round()(파이썬 기본은 은행가 반올림이라 26.5 → 26으로만
    치우친다) 대신 floor·ceil 둘 다 포함한다 — 예: 26.5 → {26.5, 26.0, 27.0}. 정수처럼
    이미 반올림이 무의미한 값(floor==ceil==value)은 자연히 한 값으로 수렴한다.
    """
    equivalents: set[float] = set()
    for candidate in (float(value), abs(float(value))):
        equivalents.add(round(candidate, 1))
        equivalents.add(round(float(math.floor(candidate)), 1))
        equivalents.add(round(float(math.ceil(candidate)), 1))
    return equivalents


def _walk_leaves(value):
    """중첩 dict/list를 재귀 순회하며 컨테이너가 아닌 리프 값만 yield한다.

    evidence.model_dump() 트리 전체에 적용하는 범용 순회다 — 필드를 하나씩 나열하지
    않으므로 RiskEvidence 스키마가 확장돼도 이 함수를 고칠 필요 없이 새 필드까지 자동으로
    훑는다(리뷰 F2: "필드 나열이 아니라 객체 순회로 구현").
    """
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_leaves(item)
    else:
        yield value


def _evidence_date_pool(evidence: RiskEvidence) -> set[str]:
    """evidence 전체에서 재귀 파생한 ISO 날짜 집합(리뷰 F2).

    evidence.model_dump() 트리의 모든 리프를 훑어, 문자열 리프마다 그 안에 등장하는 ISO
    날짜 토큰을 전부 모은다 — as_of·depletion_date처럼 그 자체가 날짜인 필드는 물론,
    anomalies[].detail 같은 자유 텍스트 안에 "부수적으로" 언급된 날짜(예: "입고 예정
    2026-07-18 대비 14일 지연")까지 포함한다. 근거 안에 실재하는 사실은 절대 플래그하지
    않는다는 원칙을 지키기 위해, 손으로 나열한 날짜 필드 목록 대신 트리 전체를 본다.
    """
    dates: set[str] = set()
    for leaf in _walk_leaves(evidence.model_dump()):
        if isinstance(leaf, str):
            dates |= set(_dates_in_text(leaf))
    return dates


def _evidence_number_pool(evidence: RiskEvidence) -> set[float]:
    """evidence 전체에서 재귀 파생한 "허용 수치 표현" 집합(리뷰 F2).

    evidence.model_dump() 트리의 모든 리프를 훑어, 수치 리프(bool 제외 — bool은 int의
    서브클래스라 escalated_by_notice의 True/False가 0/1로 오인되지 않도록 명시적으로
    건너뛴다)는 곧바로, 문자열 리프는 그 안의 2자리 이상 숫자 토큰을 추출해
    _numeric_equivalents로 각각 반올림·절대값·양방향 정수화 동치를 더한다. score·
    current_stock 같은 구조화 필드뿐 아니라 anomalies[].detail·item_name·substitutes의
    item_name·current_stock 등 evidence가 공급한 문자열 안의 숫자까지 전부 포함된다 —
    스키마가 확장되면 이 풀도 코드 수정 없이 자동으로 따라간다.
    """
    pool: set[float] = set()
    for leaf in _walk_leaves(evidence.model_dump()):
        if isinstance(leaf, bool):
            continue
        if isinstance(leaf, (int, float)):
            pool |= _numeric_equivalents(leaf)
        elif isinstance(leaf, str):
            for token in _number_tokens_in_text(leaf):
                pool |= _numeric_equivalents(float(token))
    return pool


def verify_explanation_grounding(evidence: RiskEvidence, explanation: RiskExplanation) -> list[str]:
    """생성물(explanation)이 근거(evidence) 밖 사실을 말했는지 결정적으로 대조한다.

    LLM은 관여하지 않는다 — 위반마다 "{종류}: {상세}" 형식의 플래그 문자열 하나를 만들고,
    5종 검사를 항상 고정 순서(unknown_ref → empty_refs → unsupported_date →
    unsupported_number → phantom_notice)로 실행해 반환 리스트의 순서까지 결정적이다.
    한 검사 안에서 여러 위반이 나오면(예: 근거 밖 ID를 여러 개 인용) 본문에 처음 등장한
    순서로 각각 플래그 하나씩 만든다(중복은 1건으로 합친다). 위반이 있어도 예외를 던지지
    않는다 — 호출부(M-21)가 hallucination_flags로 결과에 그대로 부착한다.

    날짜·수치 대조 풀은 evidence 전체를 재귀 순회해(_evidence_date_pool·
    _evidence_number_pool, evidence.model_dump() 트리 — 손으로 나열한 필드가 아님) 모든
    수치·날짜 필드는 물론 anomalies[].detail·item_name 등 evidence가 공급한 문자열 안의
    숫자·ISO 날짜 토큰까지 포함한다(리뷰 F2) — 스키마가 확장돼도 이 함수를 고칠 필요가
    없다.

    **구조적 한계(과대 서술 금지)**: 이 대조기는 "그 값이 evidence 어딘가에 존재하는가"만
    본다 — 본문이 그 값에 부여한 **역할**이 evidence 안에서의 역할과 같은지는 확인하지
    않는다(role-blind). 예를 들어 evidence의 shipment qty가 200일 때 본문이 "현재 재고
    200개"라고 썼다면(실제로는 입고 예정 수량이지 재고가 아니다) 200이라는 숫자 자체는
    evidence 안에 있으므로 unsupported_number로 잡히지 않는다 — 이런 교차 인용
    (cross-citation) 오류는 5종 플래그 중 어떤 것도 방어하지 않으며, 이 함수의 탐지
    범위를 벗어난다.
    """
    flags: list[str] = []
    body = _body_text(explanation)
    allowed_refs = set(evidence.evidence_refs)

    # 1. unknown_ref — explanation.evidence_refs + 각 action.evidence_refs 전체가 대상.
    cited_refs: list[str] = list(explanation.evidence_refs)
    for action in explanation.actions:
        cited_refs.extend(action.evidence_refs)
    unknown_refs = _unique_preserve_order(ref for ref in cited_refs if ref not in allowed_refs)
    flags.extend(f"unknown_ref: {ref}" for ref in unknown_refs)

    # 2. empty_refs — 상단 evidence_refs 1건 + 빈 action 각각 1건(인용 필수 원칙).
    if not explanation.evidence_refs:
        flags.append("empty_refs: explanation.evidence_refs")
    for index, action in enumerate(explanation.actions):
        if not action.evidence_refs:
            flags.append(f"empty_refs: actions[{index}].evidence_refs ({action.title!r})")

    # 3. unsupported_date — 본문의 ISO 날짜 중 evidence 전체 재귀 파생 날짜 집합 밖의 것.
    evidence_dates = _evidence_date_pool(evidence)
    body_dates = _unique_preserve_order(_dates_in_text(body))
    flags.extend(
        f"unsupported_date: {found_date}" for found_date in body_dates if found_date not in evidence_dates
    )

    # 4. unsupported_number — 본문의 2자리 이상 숫자 토큰 중 evidence 전체 재귀 파생 수치
    #    풀 밖의 것.
    number_tokens = _unique_preserve_order(_number_tokens_in_text(body))
    allowed_numbers = _evidence_number_pool(evidence)
    flags.extend(
        f"unsupported_number: {token}"
        for token in number_tokens
        if round(float(token), 1) not in allowed_numbers
    )

    # 5. phantom_notice — 활성 공고가 전혀 없는데 본문이 '공고'를 언급.
    if not evidence.active_notices and "공고" in body:
        flags.append("phantom_notice: active_notices empty but body mentions 공고")

    return flags
