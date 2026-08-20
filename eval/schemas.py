"""JudgeScore 계약 — 루브릭 v1(``eval/rubric.md``)의 4축 평가 결과 pydantic 스키마.

바인딩 계약(``.superpowers/sdd/2026-08-19-medsupply-master-plan/briefs/task-S18-brief.md``
§바인딩 결정): groundedness·cause_relevance·actionability는 0~1 연속값, hallucination은
bool, rationale은 judge 프롬프트가 강제하는 한국어 서술이다. judge_model·rubric_version은
judge LLM이 직접 출력하는 값이 아니라, **실행기(eval/judge.py, Task S-27 몫 — 이 태스크에서는
만들지 않는다)가 호출 후에 채워 넣는 메타 필드**다.

``JudgeOutput``은 judge LLM이 실제로 생성해야 하는 JSON 구조 그 자체다 — ``JudgeScore``를
상속해 만들지 않고 ``JudgeScore``가 ``JudgeOutput``을 상속하는 방향으로 뒀다. 그래서
"judge_model·rubric_version을 제외한 평가 필드는 프롬프트 출력 스키마와 1:1"이라는 계약이
필드를 둘 다에 따로 나열하는 방식이 아니라 **상속 구조 자체로 보장**된다(어긋나면 애초에
정의가 안 된다). ``EXECUTOR_FILLED_FIELDS``는 그 차집합을 테스트가 명시적으로 검증할 수
있도록 남겨 둔 표시자다.

S-27 실행기가 이 스키마를 쓰는 방식(참고용 — 여기서 구현하지 않음): judge 프롬프트
(``eval/prompts/judge_v1.txt``) 렌더 결과를
``medsupply.llm.client.complete_json(..., schema=JudgeOutput, provider=<교차 judge
provider>)``로 호출해 5필드 JSON을 파싱한 뒤, 실제로 호출된 모델 문자열과
``eval/config.yaml``의 ``rubric_version``을 덧붙여 ``JudgeScore``를 완성한다.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class JudgeOutput(BaseModel):
    """judge LLM이 실제로 출력해야 하는 JSON 구조(``judge_v1.txt``의 출력 지시와 1:1)."""

    groundedness: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "설명의 각 주장·수치가 제공된 근거에서 직접 뒷받침되는 비율(0~1)."
            " 앵커 정의는 eval/rubric.md §1 참조."
        ),
    )
    cause_relevance: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "제시된 원인이 근거의 위험 유형·요인과 논리적으로 맞물리는 정도(0~1)."
            " 앵커 정의는 eval/rubric.md §2 참조."
        ),
    )
    actionability: float = Field(
        ge=0.0,
        le=1.0,
        description=(
            "권장 조치가 약사가 즉시 실행 가능한 구체 행동인 정도(0~1)."
            " 앵커 정의는 eval/rubric.md §3 참조."
        ),
    )
    hallucination: bool = Field(
        description=(
            "생성문이 제공된 근거에 없는 사실(수치·날짜·공고 내용·인과)을 단정하면 true."
            " 근거의 재표현·요약·단위 환산은 false(eval/rubric.md §4)."
        ),
    )
    rationale: str = Field(
        description="판정 근거. 한국어 2문장 이내, 근거 또는 생성문 표현의 짧은 인용 포함.",
    )


class JudgeScore(JudgeOutput):
    """JudgeScore 계약 전체 — ``JudgeOutput``(judge 프롬프트 출력 5필드) + 실행기가 채우는
    메타 2필드(judge_model·rubric_version). 필드 순서는 브리프 바인딩 결정과 동일하다:
    groundedness·cause_relevance·actionability·hallucination·rationale·judge_model·
    rubric_version.
    """

    judge_model: str = Field(
        description="실제로 호출된 judge 모델 식별자(실행기가 채움, 별칭이 아닌 스냅샷 지향).",
    )
    rubric_version: str = Field(
        description="적용된 루브릭 버전(실행기가 채움). 예: 'v1'.",
    )

    #: judge 프롬프트가 직접 생성하지 않고 실행기가 사후에 채우는 필드 이름 집합.
    #: ``set(JudgeScore.model_fields) - EXECUTOR_FILLED_FIELDS == set(JudgeOutput.model_fields)``
    #: 로 "프롬프트 출력 스키마와 1:1" 계약을 테스트가 고정 검증한다.
    EXECUTOR_FILLED_FIELDS: ClassVar[frozenset[str]] = frozenset({"judge_model", "rubric_version"})
