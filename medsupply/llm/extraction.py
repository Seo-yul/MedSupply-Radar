"""공고 원문 구조화 추출(M-13) — 환각 통제가 핵심인 LLM 호출 + 결정적 사후 검증.

extract_notice()는 LLM(complete_json)으로 NoticeExtraction을 채우지만, 그 결과를
곧바로 신뢰하지 않는다. 신뢰도(confidence)·확인상태(status)는 _verify(순수 함수)가
원문과 발췌를 문자열로 대조해 전적으로 LLM 밖에서 결정한다 — LLM은 "무엇을
추출했는가"만 말하고, "그 추출을 믿을 수 있는가"는 이 모듈의 결정적 코드가 판정한다.

'확인 완료'(notice_extractions.status의 세 번째 값)는 사람이 검토를 마쳤을 때만
붙는 상태이며, 이 모듈의 어떤 경로도 그 값을 반환하지 않는다 — extract_notice()와
_verify()는 '자동확정' 또는 '확인 필요'만 반환한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import complete_json
from medsupply.llm.config import load_llm_config
from medsupply.llm.prompts.loader import load_prompt
from medsupply.llm.schemas import ALLOWED_NOTICE_TYPES, NoticeExtraction
from medsupply.llm.tracing import observed

#: status 판정 임계값(브리프 고정값 + 픽스 라운드 1 컨트롤러 판정). confidence가 이
#: 값 이상이고, 미발견 quote 0건·필수 필드 결손 0건·날짜 파싱 성공까지 넷 다
#: 만족해야 '자동확정'이다 — 하나라도 어긋나면 '확인 필요'.
CONFIDENCE_THRESHOLD = 0.8

#: 프롬프트 레지스트리 task명이자 complete_json/cache의 task 라벨(고정 문자열).
_TASK = "notice_extract"

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class ExtractionResult:
    """extract_notice()의 반환값 — LLM 추출값 + 결정적 검증 결과."""

    extraction: NoticeExtraction
    confidence: float  # 0.0~1.0(결정적 산정, _verify)
    status: str  # '자동확정' | '확인 필요' — '확인 완료'는 사람 액션 전용이라 여기서 나오지 않는다.
    verification: dict  # {quotes_total, quotes_found, missing_fields, date_parse_ok, notes}
    provider: str
    model: str
    prompt_version: str
    cache_hit: bool


def _normalize_whitespace(text: str) -> str:
    """연속 공백(개행 포함)을 단일 스페이스로 축약하고 양끝을 다듬는다.

    발췌-원문 대조에 앞서 원문과 quote 양쪽에 동일하게 적용한다 — 원문의 개행·
    다중 공백과 quote의 단일 공백 표기 차이를 흡수하기 위함이다.
    """
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parses_as_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _verify(extraction: NoticeExtraction, raw_text: str) -> tuple[float, str, dict]:
    """추출 결과를 원문과 대조해 신뢰도·확인상태를 결정적으로 산정한다(LLM 미관여 순수 함수).

    검증 항목:
        - 발췌 대조: evidence_quotes 각 항목을 공백 정규화한 뒤, 공백 정규화한 원문에서
          부분 문자열로 검색한다(개행·연속 공백 차이는 무시). evidence_quotes가 비어
          있으면(quotes_total == 0) 대조할 대상이 전혀 없다는 뜻이므로, 미발견 비율을
          1.0(최대 감점)으로 취급한다 — "발췌 없음"을 "전부 검증됨"으로 오인하지
          않기 위한 보수적 처리다.
        - 필수 필드: product_names >= 1개, reason이 공백 제외 비어있지 않음, notice_type이
          ALLOWED_NOTICE_TYPES에 속함. 이 3개만 대상이며(ingredient_names 등은 대상이
          아니다), 결손 1건당 confidence 0.2를 감점한다.
        - 날짜: halt_start_date/expected_restart_date가 null이 아니면 ISO(YYYY-MM-DD)
          파싱이 가능해야 한다. 둘 중 하나라도 실패하면 date_parse_ok=False로 하고,
          날짜 필드가 몇 개 실패했든 감점은 0.1 한 번만 적용한다.

    confidence = 1.0 − 0.5×(미발견 quote 비율) − 0.2×(필수 필드 결손 수) −
    (날짜 파싱 실패 시 0.1). 하한 0.0으로 클램프한 뒤 소수 3자리로 반올림한다.

    status는 다음 네 조건을 전부 만족할 때만 '자동확정'이다: confidence >=
    CONFIDENCE_THRESHOLD, 미발견 quote 0건, missing_fields 0건, date_parse_ok=True.
    하나라도 어긋나면 '확인 필요'다(픽스 라운드 1 — 컨트롤러 판정: 필수 필드 결손·
    날짜 파싱 실패는 confidence 감점으로 희석될 신호가 아니라 발췌 불일치와 동급의
    결정적 실패 신호이며, 보수적 기본값 철학을 따른다 — confidence 산식 자체는
    이 판정으로 바뀌지 않는다). '확인 완료'는 이 함수가 절대 반환하지 않는다 —
    사람이 검토를 마쳤을 때만 상위 계층(사람 액션)이 붙이는 상태다.

    Args:
        extraction: LLM이 채운 NoticeExtraction(구조만 유효, 내용은 아직 미검증).
        raw_text: 대조 대상 공고 원문(가공 없이 그대로).

    Returns:
        (confidence, status, verification) 튜플. verification은
        {"quotes_total", "quotes_found", "missing_fields", "date_parse_ok", "notes"}.
    """
    normalized_raw_text = _normalize_whitespace(raw_text)

    quotes_total = len(extraction.evidence_quotes)
    quotes_found = sum(
        1
        for quote in extraction.evidence_quotes
        if _normalize_whitespace(quote) in normalized_raw_text
    )

    notes: list[str] = []

    if quotes_total == 0:
        missing_quote_ratio = 1.0
        notes.append("evidence_quotes가 비어 있어 발췌-원문 대조를 할 수 없다.")
    else:
        missing_quote_ratio = (quotes_total - quotes_found) / quotes_total
        if quotes_found < quotes_total:
            notes.append(
                f"원문에서 찾지 못한 발췌 {quotes_total - quotes_found}/{quotes_total}건."
            )

    missing_fields: list[str] = []
    if len(extraction.product_names) < 1:
        missing_fields.append("product_names")
    if not extraction.reason.strip():
        missing_fields.append("reason")
    if extraction.notice_type not in ALLOWED_NOTICE_TYPES:
        missing_fields.append("notice_type")
    if missing_fields:
        notes.append(f"필수 필드 결손: {', '.join(missing_fields)}.")

    date_parse_ok = True
    for field_name, value in (
        ("halt_start_date", extraction.halt_start_date),
        ("expected_restart_date", extraction.expected_restart_date),
    ):
        if value is not None and not _parses_as_iso_date(value):
            date_parse_ok = False
            notes.append(f"{field_name} 날짜 파싱 실패: {value!r}.")

    confidence = 1.0
    confidence -= 0.5 * missing_quote_ratio
    confidence -= 0.2 * len(missing_fields)
    if not date_parse_ok:
        confidence -= 0.1
    confidence = round(max(0.0, confidence), 3)

    quotes_all_found = quotes_found == quotes_total
    if confidence >= CONFIDENCE_THRESHOLD and quotes_all_found and not missing_fields and date_parse_ok:
        status = "자동확정"
    else:
        status = "확인 필요"

    verification = {
        "quotes_total": quotes_total,
        "quotes_found": quotes_found,
        "missing_fields": missing_fields,
        "date_parse_ok": date_parse_ok,
        "notes": notes,
    }

    return confidence, status, verification


@observed("notice_extract")
def extract_notice(
    raw_text: str,
    *,
    notice_id: str | None = None,
    prompt_version: str | None = None,
    force_refresh: bool = False,
) -> ExtractionResult:
    """공고 원문에서 구조화 정보를 추출하고, LLM 밖의 결정적 코드로 신뢰도를 매긴다.

    notice_id는 v1에서 추출 로직에 관여하지 않는다 — cache_key는 raw_text·프롬프트
    버전·모델에만 의존하므로(동일 원문이면 notice_id가 달라도 같은 캐시를 공유한다),
    notice_id는 호출부(예: 후속 태스크의 로깅·영속화)가 이미 이 시그니처로 넘길 수
    있도록 남겨둔 계약상 자리다.

    Args:
        raw_text: 공고 원문(가공 없이 그대로).
        notice_id: (v1 미사용, 계약상 자리) 공고 ID.
        prompt_version: None이면 프롬프트 레지스트리의 active 버전을 쓴다.
        force_refresh: True면 캐시를 무시하고 항상 재호출한 뒤 캐시를 덮어쓴다
            (complete_json에 그대로 전파).

    Returns:
        ExtractionResult. status는 '자동확정' 또는 '확인 필요'만 나온다 — '확인 완료'는
        사람이 검토를 마쳤을 때만 붙는 상태라 이 함수는 절대 반환하지 않는다.
    """
    template = load_prompt(_TASK, prompt_version)
    rendered = template.render(raw_text=raw_text)

    cfg = load_llm_config()
    cache_key = build_cache_key(
        _TASK, rendered.version, cfg.anthropic_model, NoticeExtraction, {"raw_text": raw_text}
    )

    result = complete_json(
        _TASK,
        rendered,
        NoticeExtraction,
        cache_key=cache_key,
        force_refresh=force_refresh,
    )

    confidence, status, verification = _verify(result.data, raw_text)

    return ExtractionResult(
        extraction=result.data,
        confidence=confidence,
        status=status,
        verification=verification,
        provider=result.provider,
        model=result.model,
        prompt_version=rendered.version,
        cache_hit=result.cache_hit,
    )
