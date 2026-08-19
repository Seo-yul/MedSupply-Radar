"""LLM 공급자 설정 로더.

공급자·모드는 환경변수로 결정된다(사용자 확정 결정):
- LLM_PROVIDER: 'auto'(기본)|'anthropic'|'openai'
- LLM_MODE: 'online'(기본)|'offline'

API 키 자체는 이 모듈이 보관하지 않는다 — Anthropic/OpenAI SDK가 각각
ANTHROPIC_API_KEY/OPENAI_API_KEY를 환경에서 직접 읽는다. 여기서는 키가
설정되어 있는지 여부(bool)만 노출한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# 모듈 최초 import 시 1회만 수행 — 이미 설정된 환경변수는 덮어쓰지 않는다(override=False).
load_dotenv(override=False)

_VALID_PROVIDERS = ("auto", "anthropic", "openai")
_VALID_MODES = ("online", "offline")

_DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
_DEFAULT_OPENAI_MODEL = "gpt-5"


@dataclass(frozen=True)
class LLMConfig:
    """LLM 호출 계층의 실행 시점 설정 스냅샷."""

    provider: str  # 'auto'|'anthropic'|'openai'
    mode: str  # 'online'|'offline'
    anthropic_model: str
    openai_model: str
    anthropic_key_set: bool
    openai_key_set: bool


def load_llm_config() -> LLMConfig:
    """환경변수로부터 LLMConfig를 로드한다.

    허용되지 않는 provider/mode 값은 ValueError를 발생시킨다(오타 방지).
    """
    provider = os.environ.get("LLM_PROVIDER", "auto")
    if provider not in _VALID_PROVIDERS:
        raise ValueError(f"LLM_PROVIDER={provider!r}는 허용되지 않습니다(허용값: {_VALID_PROVIDERS})")

    mode = os.environ.get("LLM_MODE", "online")
    if mode not in _VALID_MODES:
        raise ValueError(f"LLM_MODE={mode!r}는 허용되지 않습니다(허용값: {_VALID_MODES})")

    return LLMConfig(
        provider=provider,
        mode=mode,
        anthropic_model=os.environ.get("ANTHROPIC_MODEL") or _DEFAULT_ANTHROPIC_MODEL,
        openai_model=os.environ.get("OPENAI_MODEL") or _DEFAULT_OPENAI_MODEL,
        anthropic_key_set=bool(os.environ.get("ANTHROPIC_API_KEY")),
        openai_key_set=bool(os.environ.get("OPENAI_API_KEY")),
    )
