"""공고 구조화 추출 스키마 — notice_extractions.payload_json의 실체(pydantic).

docs/data-model.md §6(payload_json 필드 초안)의 확정판이다(task M-13). 이 모델은
LLM 구조화 출력(complete_json의 output_format)에 그대로 쓰이므로, 필드 자체에는
값 제약(min_length 등)을 걸지 않는다 — 예를 들어 product_names가 빈 리스트여도
이 모델은 유효하게 파싱된다. "필수 필드 결손"을 신뢰도 감점·확인상태 강등으로
연결하는 판정은 medsupply.llm.extraction._verify(LLM 밖의 결정적 코드)의 몫이다.
여기서 pydantic 검증으로 막아버리면 그 결손이 감점·확인 필요로 내려가는 대신
파싱 예외로 날아가 버려, 환각 통제 설계(발췌-원문 대조는 전부 결정적 코드)와
정면으로 배치된다.
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
