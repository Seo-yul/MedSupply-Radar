"""Anthropic(기본)·OpenAI(폴백) 이중화 JSON 구조화 호출 계층.

공급자 선택 정책은 medsupply.llm.config.load_llm_config()가 결정한다(환경변수
LLM_PROVIDER/LLM_MODE). 캐시·프롬프트 레지스트리·tracing은 후속 태스크이며,
여기서는 계약(cache_key, trace_id, cache_hit)만 자리를 남겨둔다.

SDK 호출 패턴은 태스크 브리프에 명시된 형태를 그대로 따른다 — 특히 Anthropic
호출에는 temperature 등 샘플링 파라미터를 넣지 않는다(claude-opus-5에서 제거되어
400이 발생한다).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import anthropic
import openai
from pydantic import BaseModel

from medsupply.llm.config import LLMConfig, load_llm_config

T = TypeVar("T", bound=BaseModel)

_OFFLINE_MESSAGE = "오프라인 모드는 워밍된 캐시가 필요합니다(후속 태스크에서 지원)"

# 모듈 수준 캐시(지연 생성) — 클라이언트 객체를 프로세스 내에서 재사용한다.
_anthropic_client: anthropic.Anthropic | None = None
_openai_client: openai.OpenAI | None = None


@dataclass(frozen=True)
class RenderedPrompt:
    """렌더링된 프롬프트(시스템/사용자 메시지 + 프롬프트 버전 라벨)."""

    system: str
    user: str
    version: str


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    """complete_json 성공 결과."""

    data: T
    provider: str
    model: str
    cache_hit: bool
    latency_ms: int
    trace_id: str | None
    usage: dict  # {"input_tokens": int, "output_tokens": int} 정규화


class LLMUnavailableError(RuntimeError):
    """두 공급자 모두 실패했거나, 폴백 대상 공급자의 키가 설정되지 않은 경우."""


class LLMOfflineError(RuntimeError):
    """offline 모드인데 캐시가 아직 구현되지 않은 경우(캐시는 후속 태스크)."""


def _get_anthropic_client() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic()  # 키는 env(ANTHROPIC_API_KEY)에서
    return _anthropic_client


def _get_openai_client() -> openai.OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = openai.OpenAI()  # 키는 env(OPENAI_API_KEY)에서
    return _openai_client


def _call_anthropic(
    prompt: RenderedPrompt, schema: type[T], model: str, max_tokens: int
) -> tuple[T, str, dict]:
    client = _get_anthropic_client()
    resp = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=prompt.system,
        messages=[{"role": "user", "content": prompt.user}],
        output_format=schema,
    )
    data = resp.parsed_output
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return data, resp.model, usage


def _call_openai(prompt: RenderedPrompt, schema: type[T], model: str) -> tuple[T, str, dict]:
    client = _get_openai_client()
    comp = client.chat.completions.parse(
        model=model,
        messages=[
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        response_format=schema,
    )
    data = comp.choices[0].message.parsed
    usage = {
        "input_tokens": comp.usage.prompt_tokens,
        "output_tokens": comp.usage.completion_tokens,
    }
    return data, comp.model, usage


def _is_anthropic_fallback_error(exc: BaseException) -> bool:
    """폴백 트리거 여부(브리프 규칙 그대로).

    RateLimitError · AuthenticationError · APIConnectionError · APIStatusError(status>=500)만
    폴백 대상이다. BadRequestError를 포함한 그 외 4xx(APIStatusError, status<500)는 같은 요청이
    OpenAI에서도 실패할 가능성이 높고 원인을 은폐하므로 폴백하지 않는다.
    """
    if isinstance(exc, (anthropic.RateLimitError, anthropic.AuthenticationError)):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def complete_json(
    task: str,
    prompt: RenderedPrompt,
    schema: type[T],
    *,
    provider: Literal["anthropic", "openai"] | None = None,
    max_tokens: int = 8192,
    cache_key: str | None = None,
) -> LLMResult[T]:
    """Anthropic 우선·OpenAI 폴백으로 JSON 구조화 호출을 수행한다.

    Args:
        task: 로깅·(후속) tracing 라벨. 이 태스크에서는 저장하지 않는다.
        prompt: 렌더링된 system/user 프롬프트 + 버전.
        schema: 응답을 검증할 pydantic BaseModel 서브클래스.
        provider: 명시하면 해당 공급자만 시도하고 폴백하지 않는다. None이면
            LLMConfig.provider(환경변수 LLM_PROVIDER)를 따른다.
        max_tokens: Anthropic 호출의 max_tokens(OpenAI 쪽은 SDK 기본값을 따른다).
        cache_key: v1에서는 사용하지 않는다(받기만 함) — 캐시 계층(및 이를 통한
            cache_hit=True 반환)은 후속 태스크에서 지원한다.

    Raises:
        LLMOfflineError: LLM_MODE=offline인데 캐시가 아직 없는 경우.
        LLMUnavailableError: 두 공급자 모두 실패했거나, 폴백 대상 공급자의 키가
            설정되지 않아 폴백을 시도할 수 없는 경우.
        anthropic.BadRequestError 등: 폴백 대상이 아닌 예외는 그대로 전파된다.
    """
    cfg = load_llm_config()

    if cfg.mode == "offline":
        raise LLMOfflineError(_OFFLINE_MESSAGE)

    resolved_provider = provider if provider is not None else cfg.provider

    start = time.monotonic()

    if resolved_provider == "anthropic":
        if not cfg.anthropic_key_set:
            raise LLMUnavailableError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        data, model, usage = _call_anthropic(prompt, schema, cfg.anthropic_model, max_tokens)
        used_provider = "anthropic"
    elif resolved_provider == "openai":
        if not cfg.openai_key_set:
            raise LLMUnavailableError("OPENAI_API_KEY가 설정되지 않았습니다.")
        data, model, usage = _call_openai(prompt, schema, cfg.openai_model)
        used_provider = "openai"
    elif resolved_provider == "auto":
        data, model, usage, used_provider = _complete_json_auto(prompt, schema, cfg, max_tokens)
    else:  # pragma: no cover - load_llm_config()가 이미 검증하므로 도달하지 않음
        raise ValueError(f"알 수 없는 provider: {resolved_provider!r}")

    latency_ms = int((time.monotonic() - start) * 1000)

    return LLMResult(
        data=data,
        provider=used_provider,
        model=model,
        cache_hit=False,
        latency_ms=latency_ms,
        trace_id=None,
        usage=usage,
    )


def _complete_json_auto(
    prompt: RenderedPrompt, schema: type[T], cfg: LLMConfig, max_tokens: int
) -> tuple[T, str, dict, str]:
    """LLM_PROVIDER=auto 정책: Anthropic 우선, 자격이 되는 예외에 한해 OpenAI로 폴백."""
    if not cfg.anthropic_key_set:
        if not cfg.openai_key_set:
            raise LLMUnavailableError("ANTHROPIC_API_KEY, OPENAI_API_KEY가 모두 설정되지 않았습니다.")
        data, model, usage = _call_openai(prompt, schema, cfg.openai_model)
        return data, model, usage, "openai"

    try:
        data, model, usage = _call_anthropic(prompt, schema, cfg.anthropic_model, max_tokens)
        return data, model, usage, "anthropic"
    except Exception as anthropic_exc:
        if not _is_anthropic_fallback_error(anthropic_exc):
            raise

        if not cfg.openai_key_set:
            raise LLMUnavailableError(
                f"Anthropic 호출 실패({anthropic_exc!r})했고, OpenAI 폴백은 OPENAI_API_KEY 미설정으로"
                " 시도할 수 없습니다."
            ) from anthropic_exc

        try:
            data, model, usage = _call_openai(prompt, schema, cfg.openai_model)
            return data, model, usage, "openai"
        except Exception as openai_exc:
            raise LLMUnavailableError(
                f"두 공급자 모두 실패했습니다 - anthropic: {anthropic_exc!r}; openai: {openai_exc!r}"
            ) from openai_exc
