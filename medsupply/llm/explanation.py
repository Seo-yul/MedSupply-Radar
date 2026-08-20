"""위험 원인 설명·대응방안 생성 v1 + 영속화(M-21) — generate_risk_explanation · explain_item.

RiskEvidence(M-20)를 입력으로 원인 설명+대응방안(RiskExplanation)을 LLM(complete_json)으로
채우고, 그 직후 결정적 사후 대조기(medsupply.llm.grounding.verify_explanation_grounding)를
통과시켜 hallucination_flags를 붙인다. 대조 위반은 추출(M-13)의 '확인 필요' 강등과 다르게
결과를 낮추지 않는다 — flags만 부착해 그대로 반환·영속화하고, 그 표시는 후속 태스크(M-23)
몫이다.

생성물에는 등급·점수를 다시 판정시키지 않는다(RiskExplanation에 그 필드가 아예 없다 —
마스터 플랜 결정 38) — 위험 판정은 risk_results가 이미 결정적으로 끝냈고, 이 모듈은 "왜"와
"그래서 무엇을 확인할지"만 LLM에게 채우게 한다. 캐시 키 조립(build_cache_key)·오프라인
동작은 medsupply.llm.extraction과 동일한 M-12 계층 관례를 그대로 따른다(cache_key는
evidence·history 조합에 대해 결정적이다).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from medsupply.data import queries, writer
from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import complete_json
from medsupply.llm.config import load_llm_config
from medsupply.llm.grounding import collect_risk_evidence, verify_explanation_grounding
from medsupply.llm.prompts.loader import load_prompt
from medsupply.llm.schemas import RiskEvidence, RiskExplanation
from medsupply.llm.tracing import observed

#: 프롬프트 레지스트리 task명이자 complete_json/cache의 task 라벨(고정 문자열).
_TASK = "risk_explain"

#: history 각 건에서 프롬프트·cache_key에 남기는 필드만(history_id·item_id·owner·order_id
#: 등 나머지는 근거 설명에 불필요한 잡음이라 잘라낸다).
_HISTORY_FIELDS = ("created_at", "action_type", "note", "status")

#: generate_risk_explanation이 history_json/cache_key에 반영하는 최대 건수.
_HISTORY_LIMIT = 3


@dataclass(frozen=True)
class ExplanationResult:
    """generate_risk_explanation()/explain_item()의 반환값 — LLM 생성값 + 결정적 대조 결과."""

    explanation: RiskExplanation
    hallucination_flags: tuple[str, ...]  # verify_explanation_grounding 결과(위반이어도 예외 없음)
    provider: str
    model: str
    prompt_version: str
    cache_hit: bool


def _trim_history(history: Sequence[dict]) -> list[dict]:
    """history를 최근 _HISTORY_LIMIT건만, _HISTORY_FIELDS 필드만 추려 반환한다.

    history는 caller가 이미 최신순(가장 최근이 0번째)으로 정렬해 전달한다고 가정한다
    (medsupply.data.queries.list_action_history 기본 정렬 — created_at DESC와 동일) —
    이 함수는 재정렬하지 않고 앞 _HISTORY_LIMIT개만 취한다. 각 건에 필드가 없어도
    KeyError 없이 None으로 채운다.
    """
    trimmed = list(history)[:_HISTORY_LIMIT]
    return [{field: record.get(field) for field in _HISTORY_FIELDS} for record in trimmed]


@observed("risk_explain")
def generate_risk_explanation(
    evidence: RiskEvidence,
    *,
    history: Sequence[dict] = (),
    prompt_version: str | None = None,
    force_refresh: bool = False,
) -> ExplanationResult:
    """RiskEvidence(+이력)로 RiskExplanation을 생성하고 결정적 대조기로 flags를 붙인다.

    prompt_version이 None이면 프롬프트 레지스트리(risk_explain)의 active 버전을 쓴다.
    cache_key는 evidence.model_dump()와 (최근 _HISTORY_LIMIT건·_HISTORY_FIELDS만 추린)
    history에 대해 결정적이다 — 동일 evidence·history면 항상 동일 키를 낸다
    (medsupply.llm.extraction.extract_notice와 동일한 M-12 계층 관례).

    history가 비어 있지 않은데 응답의 explanation.history_note가 None이어도 강제로 채우지
    않는다 — 과거 유사 대응 언급 여부는 모델 재량이다(브리프 §generate_risk_explanation
    동작 4).

    Args:
        evidence: medsupply.llm.grounding.collect_risk_evidence가 조립한 closed-world 근거.
        history: 최신순으로 정렬된 과거 조치 이력(dict 목록, 보통
            medsupply.data.queries.list_action_history의 결과 레코드). 비어 있어도 된다.
        prompt_version: None이면 레지스트리의 active 버전(risk_explain)을 쓴다.
        force_refresh: True면 캐시를 무시하고 항상 재호출한 뒤 캐시를 덮어쓴다
            (complete_json에 그대로 전파).

    Returns:
        ExplanationResult. hallucination_flags는 위반이 없으면 빈 튜플이다(예외를 던지지
        않는다 — verify_explanation_grounding 계약 그대로).
    """
    trimmed_history = _trim_history(history)
    evidence_dump = evidence.model_dump()

    template = load_prompt(_TASK, prompt_version)
    rendered = template.render(
        evidence_json=json.dumps(evidence_dump, ensure_ascii=False),
        history_json=json.dumps(trimmed_history, ensure_ascii=False),
    )

    cfg = load_llm_config()
    cache_key = build_cache_key(
        _TASK,
        rendered.version,
        cfg.anthropic_model,
        RiskExplanation,
        {"evidence": evidence_dump, "history": trimmed_history},
    )

    result = complete_json(
        _TASK,
        rendered,
        RiskExplanation,
        cache_key=cache_key,
        force_refresh=force_refresh,
    )

    flags = verify_explanation_grounding(evidence, result.data)

    return ExplanationResult(
        explanation=result.data,
        hallucination_flags=tuple(flags),
        provider=result.provider,
        model=result.model,
        prompt_version=rendered.version,
        cache_hit=result.cache_hit,
    )


def explain_item(
    conn: sqlite3.Connection, item_id: str, *, force_refresh: bool = False
) -> ExplanationResult:
    """근거 수집 → 생성 → 영속화를 원콜로 수행하는 앱 소비 진입점.

    collect_risk_evidence(conn, item_id)로 근거를 조립한다(item_id의 run이 전혀 없거나
    지정 run에 item_id 행이 없으면 ValueError — 그대로 전파하며, 이 경우 LLM은 호출하지
    않는다). evidence.risk_type과 같은 과거 이력 최근 _HISTORY_LIMIT건
    (medsupply.data.queries.list_action_history)을 첨부해 generate_risk_explanation을
    호출한다. 결과는 medsupply.data.writer.save_explanation으로 영속화한다(item_id PK,
    INSERT OR REPLACE — 재호출은 멱등하게 최신 결과로 덮어쓴다). payload에는 explanation과
    hallucination_flags를 함께 저장한다 — 화면·평가가 재검증 없이 그대로 읽는다.

    Args:
        conn: medsupply.data 계층 커넥션(sqlite3.Row row_factory 가정).
        item_id: 품목 ID.
        force_refresh: generate_risk_explanation에 그대로 전파.

    Returns:
        ExplanationResult(영속화 이후 값 — 저장 자체는 반환값에 영향 없음).

    Raises:
        ValueError: collect_risk_evidence가 던지는 예외 그대로(해당 품목의 run이 없음).
    """
    evidence = collect_risk_evidence(conn, item_id)

    history_df = queries.list_action_history(
        conn, item_id=item_id, risk_type=evidence.risk_type, limit=_HISTORY_LIMIT
    )
    history = history_df.to_dict(orient="records")

    result = generate_risk_explanation(evidence, history=history, force_refresh=force_refresh)

    payload = {
        "explanation": result.explanation.model_dump(),
        "hallucination_flags": list(result.hallucination_flags),
    }
    writer.save_explanation(
        conn,
        item_id,
        payload,
        prompt_version=result.prompt_version,
        provider=result.provider,
        model=result.model,
        run_id=evidence.run_id,
    )

    return result
