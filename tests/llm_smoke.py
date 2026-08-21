"""실 API 호출 스모크 테스트 공통 게이트(코디네이터 지시 — LLM_CACHE_PATH 격리 후속).

`tests/eval/test_rubric_fixtures.py::test_smoke_real_judge_matches_expected_range`(2건,
파라미터화) · `tests/llm/test_extraction.py::test_smoke_real_extraction_on_sample_001` ·
`tests/llm/test_explanation.py::test_smoke_real_explanation_for_item1` — 저장소 전체를
`skipif`+`API_KEY` 조합으로 grep해 확인한, 실제로 Anthropic/OpenAI API를 호출하는 유일한
테스트 4건이 이 모듈의 `skip_unless_real_llm_smoke()` 하나를 공유한다. 게이트 조건을
한 곳에만 두어 4곳에 동일 로직을 복사하지 않는다.

두 조건을 AND로 요구한다:
1. `ANTHROPIC_API_KEY` 또는 `OPENAI_API_KEY`가 설정돼 있을 것(기존 조건 — 키가 없으면
   애초에 호출 자체가 불가능하다).
2. `RUN_LLM_SMOKE=1`이 명시적으로 설정돼 있을 것(신규 — 키가 있어도 기본은 skip).
   `.env`에 실제로 동작하는 키를 두고 로컬에서 개발하는 환경에서는 (1)만으로는
   평범한 `pytest tests/`(또는 `tests/eval/`·`tests/llm/`) 실행마다 매번 소액이라도
   실 API 과금이 발생한다 — 사람이 의도적으로 opt-in해야만 실행되게 해서 이를 막는다.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv

# medsupply/llm/config.py와 동일한 패턴(override=False, 모듈 최초 import 시 1회) — 이
# 모듈만 단독으로 먼저 import돼도(medsupply.llm.config가 아직 아무 데서도 import되지
# 않은 시점의 pytest 수집 순서) .env의 키가 os.environ에 반영돼 있도록 직접 로드한다.
# 그렇지 않으면 _skip_reason()의 "키 있음/없음" 판정이 이 프로세스에서 다른 모듈이
# 먼저 medsupply.llm.config를 import했는지(=우연한 수집 순서)에 좌우돼, 같은 실행
# 안에서도 파일마다 skip 사유가 달라지는(때로는 "RUN_LLM_SMOKE=1로 활성화" 대신 "키
# 없음"으로 잘못 보고하는) 불안정한 결과를 낳는다.
load_dotenv(override=False)


def _skip_reason() -> str | None:
    """지금 실행해도 되면 None, 아니면 skip 사유 문자열(둘 중 먼저 걸리는 조건의 사유)."""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    if not has_key:
        return "ANTHROPIC_API_KEY/OPENAI_API_KEY가 없으면 실 API 스모크를 건너뛴다"
    if not os.environ.get("RUN_LLM_SMOKE"):
        return (
            "RUN_LLM_SMOKE=1로 활성화해야 실 API 스모크를 실행한다"
            "(키가 있어도 기본은 skip — 소액 과금 발생)"
        )
    return None


def skip_unless_real_llm_smoke() -> pytest.MarkDecorator:
    """실 API 스모크 테스트 함수에 붙이는 공용 skipif 마커.

    사용법: ``@skip_unless_real_llm_smoke()``를 테스트 함수(또는 `parametrize`와 함께
    쓸 때는 그 위)에 붙인다. 키 미설정 또는 `RUN_LLM_SMOKE` 미설정이면 skip되고,
    reason에 어느 조건 때문인지 그대로 노출된다(`pytest -rs`로 확인 가능).
    """
    reason = _skip_reason()
    return pytest.mark.skipif(reason is not None, reason=reason or "")
