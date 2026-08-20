"""LLM 입출력 스키마 — 공고 구조화 추출(NoticeExtraction) + 위험 근거·설명(M-20).

## NoticeExtraction
docs/data-model.md §6(payload_json 필드 초안)의 확정판이다(task M-13). 이 모델은
LLM 구조화 출력(complete_json의 output_format)에 그대로 쓰이므로, 필드 자체에는
값 제약(min_length 등)을 걸지 않는다 — 예를 들어 product_names가 빈 리스트여도
이 모델은 유효하게 파싱된다. "필수 필드 결손"을 신뢰도 감점·확인상태 강등으로
연결하는 판정은 medsupply.llm.extraction._verify(LLM 밖의 결정적 코드)의 몫이다.
여기서 pydantic 검증으로 막아버리면 그 결손이 감점·확인 필요로 내려가는 대신
파싱 예외로 날아가 버려, 환각 통제 설계(발췌-원문 대조는 전부 결정적 코드)와
정면으로 배치된다.

## RiskEvidence / RiskAction / RiskExplanation (M-20)
RiskEvidence는 medsupply.llm.grounding.collect_risk_evidence가 조립하는 **closed-world
근거 패키지**다(LLM 미관여, 결정적 코드) — 원인 설명을 생성하는 LLM(M-21)이 볼 수 있는
사실의 전체 집합이며, evidence_refs가 그 안에서 인용 가능한 근거 ID 전집합이다.
RiskAction·RiskExplanation은 그 근거를 소비해 LLM이 채우는 출력 스키마다. NoticeExtraction과
같은 이유로 여기도 값 제약(actions 최소 1개 등)을 걸지 않는다 — "근거 밖 인용·인용
누락·완결성 미달" 판정은 각각 medsupply.llm.grounding.verify_explanation_grounding(환각
사후 대조)과 eval.check_completeness(완결성, 결정 38)의 몫이다. RiskExplanation에는
등급·점수 필드를 두지 않는다(결정 38 — 판정은 risk_results가 이미 결정적으로 끝냈고,
생성물은 설명·대응방안만 맡는다는 책임 분리).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

#: notice_type이 허용하는 값 집합(docs/data-model.md §2.4와 동일한 4값).
ALLOWED_NOTICE_TYPES = ("공급중단", "공급부족", "정상화", "기타")


class NoticeExtraction(BaseModel):
    """공고 원문에서 추출한 구조화 정보 + 원문 발췌 근거.

    모든 필드는 원문에서만 근거를 취해야 한다(원문에 없는 정보는 null/빈 값).
    발췌-원문 대조·신뢰도·확인상태 산정은 이 모델의 책임이 아니다 — 이 모델은
    LLM 출력의 "형태"만 규정하고, "믿을 수 있는가"는 extraction.py가 판정한다.
    """

    product_names: list[str] = Field(description="공고 대상 제품명(1개 이상 기대)")
    ingredient_names: list[str] = Field(
        description="성분명(한글 기준, 원문에 병기된 영문이 있으면 그대로 포함 가능)"
    )
    reason: str = Field(description="공급중단·부족 사유 요약(원문 어휘를 사용)")
    halt_start_date: str | None = Field(
        description="공급중단 시작일(ISO 형식 YYYY-MM-DD). 원문에 없으면 null"
    )
    expected_restart_date: str | None = Field(
        description="공급 재개 예정일(ISO 형식 YYYY-MM-DD). 원문에 없으면 null"
    )
    notice_type: str = Field(
        description=f"공고 유형 — {', '.join(ALLOWED_NOTICE_TYPES)} 중 하나"
    )
    evidence_quotes: list[str] = Field(
        description="각 추출값의 근거가 되는 원문 발췌(원문 그대로 복사, 3개 이상 기대)"
    )


class RiskEvidence(BaseModel):
    """위험 원인 설명의 입력이 되는 closed-world 근거 패키지(medsupply.llm.grounding이 조립).

    LLM(M-21)이 볼 수 있는 사실은 이 객체가 담은 것이 전부다. evidence_refs는 본문이 인용할
    수 있는 근거 ID의 전집합이며, verify_explanation_grounding이 그 밖의 ID·수치·날짜 인용을
    환각으로 잡아낸다(ID 채번 규칙은 collect_risk_evidence 문서 참조).
    """

    item_id: str
    item_name: str
    ingredient_name_kr: str | None
    as_of: str = Field(description="위험 판정 기준일(ISO YYYY-MM-DD) — 근거로 쓰인 run의 as_of")
    run_id: str

    grade: str
    score: int
    risk_type: str
    days_to_stockout: int | None
    depletion_date: str | None

    current_stock: float | None
    avg_daily_usage: float | None
    usage_change_pct: float | None = Field(
        description="최근 28일 대비 직전 28일 사용량 변화율(%, 소수 1자리). 두 창 모두 온전한"
        " 데이터가 있을 때만 계산되고, 그렇지 않으면 null"
    )

    anomalies: list[dict] = Field(description="factors_json.anomalies 그대로(kind/detected_on/metric/detail)")
    escalated_by_notice: bool
    active_notices: list[dict] = Field(
        description="{notice_id, title, notice_type, published_date, reason} — published_date"
        " 내림차순, 동률이면 notice_id 오름차순"
    )
    next_shipment: dict | None = Field(
        description="{expected_date, qty} — as_of 시점 미래 예정(pending) 건만. 연체 건은 여기"
        " 대신 anomalies의 receipt_delay가 담당한다(2주차 브랜치 리뷰 F2)"
    )
    substitutes_same_condition: list[dict] = Field(
        description="{item_id, item_name, supplier, current_stock} — item_id 오름차순"
    )

    evidence_refs: list[str] = Field(description="본문이 인용할 수 있는 근거 ID 전집합(결정적 생성)")


class RiskAction(BaseModel):
    """원인 설명에 딸린 대응방안 1건(약사 확인 행동 — 자동 실행 아님)."""

    title: str
    description: str
    evidence_refs: list[str] = Field(description="이 대응방안이 인용한 근거 ID(RiskEvidence.evidence_refs 부분집합)")


class RiskExplanation(BaseModel):
    """LLM이 채우는 원인 설명 + 대응방안 출력(등급·점수 필드 없음 — 결정 38).

    판정(등급·점수)은 risk_results가 이미 결정적으로 끝냈다 — 이 모델은 "왜"와 "그래서
    무엇을 확인할지"만 담고, 판정을 재현·수정하지 않는다.
    """

    cause_summary: str = Field(description="원인 설명(2~4문장, 근거 수치·날짜만 인용)")
    actions: list[RiskAction] = Field(description="대응방안(≥1 기대, P0)")
    evidence_refs: list[str] = Field(description="본문(cause_summary)이 인용한 근거 ID")
    history_note: str | None = Field(
        default=None, description="과거 유사 대응 참조 — 이력이 있을 때 M-21이 채운다"
    )
