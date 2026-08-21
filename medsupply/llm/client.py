"""Anthropic(기본)·OpenAI(폴백) 이중화 JSON 구조화 호출 계층.

공급자 선택 정책은 medsupply.llm.config.load_llm_config()가 결정한다(환경변수
LLM_PROVIDER/LLM_MODE). 결과 캐시(medsupply.llm.cache)는 cache_key가 주어질 때만
관여한다 — offline 모드에서는 캐시 히트가 우선이다(M-12). 프롬프트 레지스트리·
tracing은 여전히 후속 태스크이며, trace_id는 계약만 남겨둔다.

SDK 호출 패턴은 태스크 브리프에 명시된 형태를 그대로 따른다 — 특히 Anthropic
호출에는 temperature 등 샘플링 파라미터를 넣지 않는다(claude-opus-5에서 제거되어
400이 발생한다).

complete_json의 temperature 인자(Task S-27)는 이 제약을 그대로 지킨다 — 기본값 None은
기존 동작과 완전히 동일(어느 공급자에도 전달하지 않음)하고, 값이 있어도 OpenAI 호출에만
실린다. Anthropic 경로(_call_anthropic)는 temperature를 절대 받지 않는다.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import anthropic
import openai
from pydantic import BaseModel

from medsupply import settings
from medsupply.llm.cache import cache_get, cache_put
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
    """offline 모드인데 캐시로 서빙할 수 없는 경우(cache_key 없음, 또는 캐시 미스)."""


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


def _call_openai(
    prompt: RenderedPrompt, schema: type[T], model: str, temperature: float | None = None
) -> tuple[T, str, dict]:
    client = _get_openai_client()
    kwargs: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "response_format": schema,
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    comp = client.chat.completions.parse(**kwargs)
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
    temperature: float | None = None,
    max_tokens: int = 8192,
    cache_key: str | None = None,
    force_refresh: bool = False,
) -> LLMResult[T]:
    """Anthropic 우선·OpenAI 폴백으로 JSON 구조화 호출을 수행한다.

    Args:
        task: 로깅·(후속) tracing 라벨이자 캐시 항목의 task 컬럼.
        prompt: 렌더링된 system/user 프롬프트 + 버전(prompt.version이 캐시의
            prompt_version으로 저장된다).
        schema: 응답을 검증할 pydantic BaseModel 서브클래스.
        provider: 명시하면 해당 공급자만 시도하고 폴백하지 않는다. None이면
            LLMConfig.provider(환경변수 LLM_PROVIDER)를 따른다.
        temperature: 샘플링 온도(Task S-27 judge 등 결정성이 필요한 호출용). None(기본)이면
            기존 동작 그대로 어느 공급자 호출에도 포함하지 않는다. 값이 있으면 OpenAI
            호출에만 포함된다 — Anthropic(claude-opus-5)은 temperature를 지원하지 않아
            (전달 시 400, 모듈 docstring 참조) 값과 무관하게 항상 제외한다.
        max_tokens: Anthropic 호출의 max_tokens(OpenAI 쪽은 SDK 기본값을 따른다).
        cache_key: None이면 캐시에 전혀 관여하지 않는다(기존 동작 그대로). 값이
            있으면 force_refresh=False일 때 우선 cache_get을 시도해 히트 시
            즉시 반환한다(cache_hit=True) — offline 모드에서도 이 히트 검사가
            먼저 일어난다("offline에서 캐시 히트 우선"). 미스 상태에서 호출이
            성공하면 cache_put으로 저장한다.
        force_refresh: True면 cache_get을 건너뛰고 항상 공급자를 호출한 뒤
            cache_put으로 기존 캐시 항목을 덮어쓴다. cache_key가 None이면
            캐시에 관여하지 않으므로 아무 효과가 없다.

    Raises:
        LLMOfflineError: LLM_MODE=offline이고, (a) cache_key가 없거나
            (b) cache_key는 있지만 캐시 미스이거나 force_refresh=True인 경우.
            (b)의 메시지에는 워밍 누락 진단을 위해 task와 cache_key 앞 12자가
            포함된다.
        LLMUnavailableError: 두 공급자 모두 실패했거나, 폴백 대상 공급자의 키가
            설정되지 않아 폴백을 시도할 수 없는 경우.
        anthropic.BadRequestError 등: 폴백 대상이 아닌 예외는 그대로 전파된다.
    """
    cfg = load_llm_config()

    if cache_key is not None and not force_refresh:
        cached = cache_get(cache_key, schema, path=settings.LLM_CACHE_PATH)
        if cached is not None:
            return cached

    if cfg.mode == "offline":
        if cache_key is None:
            raise LLMOfflineError(_OFFLINE_MESSAGE)
        raise LLMOfflineError(
            f"{_OFFLINE_MESSAGE} (task={task}, cache_key={cache_key[:12]}...)"
        )

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
        data, model, usage = _call_openai(prompt, schema, cfg.openai_model, temperature)
        used_provider = "openai"
    elif resolved_provider == "auto":
        data, model, usage, used_provider = _complete_json_auto(
            prompt, schema, cfg, max_tokens, temperature
        )
    else:  # pragma: no cover - load_llm_config()가 이미 검증하므로 도달하지 않음
        raise ValueError(f"알 수 없는 provider: {resolved_provider!r}")

    latency_ms = int((time.monotonic() - start) * 1000)

    result = LLMResult(
        data=data,
        provider=used_provider,
        model=model,
        cache_hit=False,
        latency_ms=latency_ms,
        trace_id=None,
        usage=usage,
    )

    if cache_key is not None:
        cache_put(cache_key, task, prompt.version, result, path=settings.LLM_CACHE_PATH)

    return result


def _complete_json_auto(
    prompt: RenderedPrompt,
    schema: type[T],
    cfg: LLMConfig,
    max_tokens: int,
    temperature: float | None = None,
) -> tuple[T, str, dict, str]:
    """LLM_PROVIDER=auto 정책: Anthropic 우선, 자격이 되는 예외에 한해 OpenAI로 폴백.

    temperature는 (여기서든 폴백에서든) OpenAI 호출에만 실린다 — Anthropic 시도는 성공이든
    실패든 항상 temperature 없이 이루어진다(모듈 docstring의 claude-opus-5 제약).
    """
    if not cfg.anthropic_key_set:
        if not cfg.openai_key_set:
            raise LLMUnavailableError("ANTHROPIC_API_KEY, OPENAI_API_KEY가 모두 설정되지 않았습니다.")
        data, model, usage = _call_openai(prompt, schema, cfg.openai_model, temperature)
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
            data, model, usage = _call_openai(prompt, schema, cfg.openai_model, temperature)
            return data, model, usage, "openai"
        except Exception as openai_exc:
            raise LLMUnavailableError(
                f"두 공급자 모두 실패했습니다 - anthropic: {anthropic_exc!r}; openai: {openai_exc!r}"
            ) from openai_exc
