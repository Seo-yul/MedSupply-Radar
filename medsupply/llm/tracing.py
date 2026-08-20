"""Langfuse tracing 훅 — LLM 호출 관측 단일 구현(마스터 플랜 결정 35, Task M-22).

## 절대 요건: no-op 안전
LANGFUSE_PUBLIC_KEY·LANGFUSE_SECRET_KEY·LANGFUSE_HOST 세 환경변수가 전부 설정되어
있고, langfuse SDK 임포트까지 성공해야만 "활성" 상태다. 그 외에는 전부 no-op이다 —
미설정, 부분 설정, (이 저장소·CI처럼) SDK 자체가 설치돼 있지 않은 경우 모두 포함된다.
langfuse는 requirements에 없는 **선택 의존성**이다(infra/langfuse/ compose는 사용자가
직접 켤 때만 의미가 있다 — infra/langfuse/README.md §4 비의존 원칙). 그래서 이 모듈은
임포트만으로 실패해서는 안 되고, SDK 임포트는 항상 함수 내부에서 지연(lazy) 수행하며
ImportError만 좁게 잡는다. 모듈 최상단에는 `import langfuse`가 전혀 없다 — 테스트가
monkeypatch로 sys.modules에 가짜 langfuse를 주입해 활성 경로를 검증할 수 있는 것도
이 지연 임포트 덕분이다(tests/llm/test_tracing.py).

## 관측 메타데이터 계약(결정 35 — EV-06 수신 검수가 이 5종을 전제한다)
record_metadata()가 결과 객체에서 duck-typing으로 뽑는 필드(없으면 생략):
    - prompt_version: 사용된 프롬프트 버전
    - provider: 실사용 공급자('anthropic'|'openai')
    - cache_hit: 캐시 히트 여부
    - usage: 토큰 사용량 dict(가능할 때만 — 예: LLMResult)
    - hallucination_flags: 환각 사후 대조 결과(있을 때만 — 예: ExplanationResult)

## 배선
medsupply.llm.extraction.extract_notice · medsupply.llm.explanation.generate_risk_explanation
에만 각각 @observed("notice_extract") / @observed("risk_explain")을 적용한다.
medsupply.llm.client.complete_json에는 걸지 않는다 — 이중 계측 방지(상위 함수 1곳만).
평가 계층(S-27)은 새 모듈을 만들지 않고 이 모듈을 그대로 재사용한다.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

#: init_tracing()이 "활성"으로 판정하기 위해 전부 설정돼 있어야 하는 환경변수(브리프 고정,
#: infra/langfuse/README.md §2와 동일한 이름).
_REQUIRED_ENV_VARS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")

#: record_metadata가 duck-typing으로 추출하는 계약 5종(마스터 플랜 결정 35).
_METADATA_FIELDS = ("prompt_version", "provider", "cache_hit", "usage", "hallucination_flags")

F = TypeVar("F", bound=Callable[..., Any])


def _env_fully_configured() -> bool:
    return all(os.environ.get(name) for name in _REQUIRED_ENV_VARS)


def init_tracing() -> bool:
    """LANGFUSE_* 세 변수가 전부 설정 + langfuse SDK 임포트 성공 시에만 True.

    그 외(미설정·부분 설정·SDK 미설치)는 전부 False(no-op 모드). 내부 상태를 캐시하지
    않는다 — 호출마다 환경변수와 SDK 가용성을 다시 확인하므로 멱등하되, 이전 호출
    결과에 갇히지 않는다(예: 테스트 도중 SDK가 sys.modules에 주입되면 다음 호출부터
    바로 반영된다).
    """
    if not _env_fully_configured():
        return False
    try:
        import langfuse  # noqa: F401 — 존재/임포트 가능 여부 확인이 유일한 목적.
    except ImportError:
        return False
    return True


def record_metadata(result: Any) -> dict:
    """result에서 관측 메타데이터 계약 5종을 duck-typing으로 추출한다(없는 필드는 생략).

    ExtractionResult·ExplanationResult·LLMResult 어느 것을 넣어도 동작한다 — 각자
    가진 필드만 뽑히고, 없는 필드(예: ExtractionResult의 usage)는 결과 dict에 아예
    나타나지 않는다. 순수 함수(부작용 없음)라 no-op 모드에서도 안전하게 호출할 수 있다.
    """
    return {field: getattr(result, field) for field in _METADATA_FIELDS if hasattr(result, field)}


def observed(task: str) -> Callable[[F], F]:
    """LLM 호출 함수용 관측 데코레이터.

    init_tracing()이 False(no-op 모드)면 원함수를 그대로 반환한다 — 새 객체를 만들지
    않으므로 래핑 오버헤드가 0이다(호출부 입장에서 데코레이트 전과 완전히 동일한 함수).
    이 판정은 데코레이트 시점(보통 모듈 최초 임포트 시점) 1회뿐이다 — 그 뒤 환경변수가
    바뀌어도 이미 결정된 래핑 여부는 바뀌지 않는다.

    활성 모드면 호출을 langfuse 스팬으로 감싸 기록한다: task를 스팬 이름으로 시작하고,
    정상 반환 시 record_metadata(반환값)을 output으로 남긴 뒤 스팬을 닫으며, 예외 발생
    시에는 스팬에 에러를 표시하되 예외 자체는 삼키지 않고 그대로 다시 던진다(스팬은
    with 블록 종료로 예외 여부와 무관하게 항상 닫힌다). functools.wraps로 원함수의
    이름·docstring·시그니처 메타데이터를 유지한다.
    """

    def decorator(func: F) -> F:
        if not init_tracing():
            return func

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            import langfuse

            client = langfuse.get_client()
            with client.start_as_current_span(name=task) as span:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    span.update(level="ERROR", status_message=str(exc))
                    raise
                span.update(output=record_metadata(result))
                return result

        return wrapper  # type: ignore[return-value]

    return decorator
