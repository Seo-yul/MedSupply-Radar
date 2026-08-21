"""medsupply.llm 이중화 클라이언트 테스트(전부 모킹 — 네트워크·실키 불요).

anthropic.Anthropic / openai.OpenAI 클래스를 monkeypatch로 페이크 객체로 대체하고,
예외는 실제 anthropic/openai 예외 클래스를 httpx 스텁 응답으로 생성해 사용한다.
"""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

import anthropic
import openai

from medsupply.llm import client as llm_client
from medsupply.llm.client import (
    LLMOfflineError,
    LLMResult,
    LLMUnavailableError,
    RenderedPrompt,
    complete_json,
)
from medsupply.llm.config import LLMConfig, load_llm_config

PROMPT = RenderedPrompt(system="system-prompt", user="user-prompt", version="echo@v1")


class Echo(BaseModel):
    value: str


# --------------------------------------------------------------------------
# 공용 픽스처
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    """매 테스트마다 관련 환경변수를 비우고 모듈 수준 클라이언트 캐시를 리셋한다."""
    for key in (
        "LLM_PROVIDER",
        "LLM_MODE",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_MODEL",
        "OPENAI_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(llm_client, "_anthropic_client", None)
    monkeypatch.setattr(llm_client, "_openai_client", None)
    yield


# --------------------------------------------------------------------------
# 예외 생성 헬퍼(실제 SDK 예외 클래스 + httpx 스텁)
# --------------------------------------------------------------------------


def _httpx_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "http://t"))


def _anthropic_status_error(cls, status: int, message: str) -> anthropic.APIStatusError:
    return cls(message=message, response=_httpx_response(status), body=None)


def _anthropic_connection_error(message: str) -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(message=message, request=httpx.Request("POST", "http://t"))


# --------------------------------------------------------------------------
# 페이크 Anthropic 클라이언트
# --------------------------------------------------------------------------


class _FakeAnthropicUsage:
    def __init__(self, input_tokens: int, output_tokens: int):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeParsedMessage:
    def __init__(self, parsed_output, model: str, usage: _FakeAnthropicUsage):
        self.parsed_output = parsed_output
        self.model = model
        self.usage = usage


class _FakeAnthropicMessages:
    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeAnthropicClient:
    def __init__(self, result=None, error: Exception | None = None):
        self.messages = _FakeAnthropicMessages(result=result, error=error)


def _forbidden_anthropic_client(*args, **kwargs):
    raise AssertionError("anthropic.Anthropic()가 호출되어서는 안 된다")


# --------------------------------------------------------------------------
# 페이크 OpenAI 클라이언트
# --------------------------------------------------------------------------


class _FakeOpenAIUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeParsedChatCompletion:
    def __init__(self, parsed, model: str, usage: _FakeOpenAIUsage):
        message = type("_Msg", (), {"parsed": parsed})()
        choice = type("_Choice", (), {"message": message})()
        self.choices = [choice]
        self.model = model
        self.usage = usage


class _FakeOpenAICompletions:
    def __init__(self, result=None, error: Exception | None = None):
        self._result = result
        self._error = error
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._result


class _FakeOpenAIClient:
    def __init__(self, result=None, error: Exception | None = None):
        completions = _FakeOpenAICompletions(result=result, error=error)
        self.chat = type("_Chat", (), {"completions": completions})()


def _forbidden_openai_client(*args, **kwargs):
    raise AssertionError("openai.OpenAI()가 호출되어서는 안 된다")


# --------------------------------------------------------------------------
# TestLoadLLMConfig
# --------------------------------------------------------------------------


class TestLoadLLMConfig:
    """환경변수 로드·검증·기본값."""

    def test_defaults_when_env_unset(self, monkeypatch):
        cfg = load_llm_config()
        assert cfg == LLMConfig(
            provider="auto",
            mode="online",
            anthropic_model="claude-opus-5",
            openai_model="gpt-5",
            anthropic_key_set=False,
            openai_key_set=False,
        )

    def test_reads_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-custom")
        monkeypatch.setenv("OPENAI_MODEL", "gpt-custom")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        cfg = load_llm_config()

        assert cfg.provider == "openai"
        assert cfg.mode == "offline"
        assert cfg.anthropic_model == "claude-custom"
        assert cfg.openai_model == "gpt-custom"
        assert cfg.anthropic_key_set is True
        assert cfg.openai_key_set is True

    def test_invalid_provider_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "azure")
        with pytest.raises(ValueError):
            load_llm_config()

    def test_invalid_mode_raises_value_error(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "batch")
        with pytest.raises(ValueError):
            load_llm_config()

    def test_config_is_frozen(self):
        cfg = load_llm_config()
        with pytest.raises(Exception):
            cfg.provider = "openai"  # type: ignore[misc]


# --------------------------------------------------------------------------
# TestCompleteJsonAutoProvider
# --------------------------------------------------------------------------


class TestCompleteJsonAutoProvider:
    """LLM_PROVIDER=auto(기본)일 때의 우선순위·폴백 정책."""

    def test_anthropic_success_path(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        fake_result = _FakeParsedMessage(
            parsed_output=Echo(value="hello"),
            model="claude-opus-5",
            usage=_FakeAnthropicUsage(input_tokens=11, output_tokens=22),
        )
        fake_client = _FakeAnthropicClient(result=fake_result)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_client)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        result = complete_json("echo_task", PROMPT, Echo, cache_key="unused-in-v1")

        assert isinstance(result, LLMResult)
        assert result.provider == "anthropic"
        assert result.model == "claude-opus-5"
        assert result.data == Echo(value="hello")
        assert result.cache_hit is False
        assert result.trace_id is None
        assert result.usage == {"input_tokens": 11, "output_tokens": 22}
        assert isinstance(result.latency_ms, int)
        assert result.latency_ms >= 0

        # SDK 호출 계약: 브리프에 명시된 파라미터 그대로 전달됐는지 확인
        assert len(fake_client.messages.calls) == 1
        call = fake_client.messages.calls[0]
        assert call["model"] == "claude-opus-5"
        assert call["max_tokens"] == 8192
        assert call["system"] == PROMPT.system
        assert call["messages"] == [{"role": "user", "content": PROMPT.user}]
        assert call["output_format"] is Echo
        assert "temperature" not in call

    def test_rate_limit_falls_back_to_openai(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        anthropic_error = _anthropic_status_error(anthropic.RateLimitError, 429, "rate limited")
        fake_anthropic = _FakeAnthropicClient(error=anthropic_error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="fallback"),
            model="gpt-5",
            usage=_FakeOpenAIUsage(prompt_tokens=33, completion_tokens=44),
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        result = complete_json("echo_task", PROMPT, Echo)

        assert result.provider == "openai"
        assert result.model == "gpt-5"
        assert result.data == Echo(value="fallback")
        assert result.usage == {"input_tokens": 33, "output_tokens": 44}

        assert len(fake_anthropic.messages.calls) == 1
        call = fake_openai.chat.completions.calls[0]
        assert call["model"] == "gpt-5"
        assert call["messages"] == [
            {"role": "system", "content": PROMPT.system},
            {"role": "user", "content": PROMPT.user},
        ]
        assert call["response_format"] is Echo

    def test_bad_request_propagates_without_fallback(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        anthropic_error = _anthropic_status_error(anthropic.BadRequestError, 400, "bad schema")
        fake_anthropic = _FakeAnthropicClient(error=anthropic_error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        with pytest.raises(anthropic.BadRequestError):
            complete_json("echo_task", PROMPT, Echo)

    def test_missing_anthropic_key_goes_straight_to_openai(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="direct"),
            model="gpt-5",
            usage=_FakeOpenAIUsage(prompt_tokens=1, completion_tokens=2),
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        result = complete_json("echo_task", PROMPT, Echo)

        assert result.provider == "openai"
        assert len(fake_openai.chat.completions.calls) == 1

    def test_both_providers_fail_raises_unavailable_with_both_causes(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        anthropic_error = _anthropic_status_error(anthropic.RateLimitError, 429, "anthropic-fail-marker")
        fake_anthropic = _FakeAnthropicClient(error=anthropic_error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)

        openai_error = RuntimeError("openai-fail-marker")
        fake_openai = _FakeOpenAIClient(error=openai_error)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        with pytest.raises(LLMUnavailableError) as exc_info:
            complete_json("echo_task", PROMPT, Echo)

        message = str(exc_info.value)
        assert "anthropic-fail-marker" in message
        assert "openai-fail-marker" in message

    def test_no_keys_set_raises_unavailable(self, monkeypatch):
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        with pytest.raises(LLMUnavailableError):
            complete_json("echo_task", PROMPT, Echo)

    def test_rate_limit_then_missing_openai_key_raises_unavailable(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        # OPENAI_API_KEY 미설정

        anthropic_error = _anthropic_status_error(anthropic.RateLimitError, 429, "rate limited")
        fake_anthropic = _FakeAnthropicClient(error=anthropic_error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        with pytest.raises(LLMUnavailableError):
            complete_json("echo_task", PROMPT, Echo)

    @pytest.mark.parametrize(
        "make_error",
        [
            lambda: _anthropic_status_error(anthropic.RateLimitError, 429, "m"),
            lambda: _anthropic_status_error(anthropic.AuthenticationError, 401, "m"),
            lambda: _anthropic_status_error(anthropic.InternalServerError, 500, "m"),
            lambda: _anthropic_status_error(anthropic.InternalServerError, 503, "m"),
            lambda: _anthropic_connection_error("m"),
        ],
        ids=["rate_limit_429", "auth_401", "server_500", "server_503", "connection"],
    )
    def test_qualifying_errors_trigger_fallback(self, monkeypatch, make_error):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        fake_anthropic = _FakeAnthropicClient(error=make_error())
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="fallback"),
            model="gpt-5",
            usage=_FakeOpenAIUsage(prompt_tokens=1, completion_tokens=1),
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        result = complete_json("echo_task", PROMPT, Echo)
        assert result.provider == "openai"

    @pytest.mark.parametrize(
        "make_error",
        [
            lambda: _anthropic_status_error(anthropic.BadRequestError, 400, "m"),
            lambda: _anthropic_status_error(anthropic.NotFoundError, 404, "m"),
            lambda: _anthropic_status_error(anthropic.PermissionDeniedError, 403, "m"),
        ],
        ids=["bad_request_400", "not_found_404", "permission_denied_403"],
    )
    def test_non_qualifying_errors_propagate_without_fallback(self, monkeypatch, make_error):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        error = make_error()
        fake_anthropic = _FakeAnthropicClient(error=error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        with pytest.raises(type(error)):
            complete_json("echo_task", PROMPT, Echo)


# --------------------------------------------------------------------------
# TestCompleteJsonExplicitProvider
# --------------------------------------------------------------------------


class TestCompleteJsonExplicitProvider:
    """provider 인자를 명시하면 폴백 없이 해당 공급자만 시도한다."""

    def test_forced_openai_never_calls_anthropic(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="forced"),
            model="gpt-5",
            usage=_FakeOpenAIUsage(prompt_tokens=1, completion_tokens=1),
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        result = complete_json("echo_task", PROMPT, Echo, provider="openai")

        assert result.provider == "openai"
        assert result.data == Echo(value="forced")

    def test_forced_openai_failure_propagates_without_fallback(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        openai_error = RuntimeError("openai-explicit-fail")
        fake_openai = _FakeOpenAIClient(error=openai_error)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        with pytest.raises(RuntimeError, match="openai-explicit-fail"):
            complete_json("echo_task", PROMPT, Echo, provider="openai")

    def test_forced_anthropic_ignores_openai_even_when_provider_env_is_openai(self, monkeypatch):
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        fake_result = _FakeParsedMessage(
            parsed_output=Echo(value="forced-anthropic"),
            model="claude-opus-5",
            usage=_FakeAnthropicUsage(input_tokens=5, output_tokens=6),
        )
        fake_anthropic = _FakeAnthropicClient(result=fake_result)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)

        result = complete_json("echo_task", PROMPT, Echo, provider="anthropic")

        assert result.provider == "anthropic"
        assert result.data == Echo(value="forced-anthropic")

    def test_forced_provider_missing_key_raises_unavailable(self, monkeypatch):
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        with pytest.raises(LLMUnavailableError):
            complete_json("echo_task", PROMPT, Echo, provider="anthropic")


# --------------------------------------------------------------------------
# TestOfflineMode
# --------------------------------------------------------------------------


class TestOfflineMode:
    def test_offline_mode_raises_before_any_provider_call(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)
        monkeypatch.setattr(openai, "OpenAI", _forbidden_openai_client)

        with pytest.raises(LLMOfflineError):
            complete_json("echo_task", PROMPT, Echo)


# --------------------------------------------------------------------------
# TestCompleteJsonTemperature(Task S-27 §바인딩 결정) — temperature 인자 추가.
#
# 기본값 None은 기존 동작과 완전히 동일해야 한다(무회귀). 값이 있으면 OpenAI 호출에만
# 실리고, Anthropic(claude-opus-5)은 모듈 docstring이 명시한 제약(temperature 전달 시
# 400)을 그대로 지키기 위해 항상 제외된다 — judge_generation(S-27)이 교차 규칙상 judge를
# anthropic으로 보낼 수도 있으므로, 이 제외는 온라인 실행에서 실제로 의미가 있다.
# --------------------------------------------------------------------------


class TestCompleteJsonTemperature:
    def test_default_none_omits_temperature_from_anthropic_call(self, monkeypatch):
        """기존 test_anthropic_success_path와 동일한 무회귀 확인 — temperature 인자를 아예
        넘기지 않아도 여전히 anthropic 호출 kwargs에 temperature가 없다."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_result = _FakeParsedMessage(
            parsed_output=Echo(value="hello"),
            model="claude-opus-5",
            usage=_FakeAnthropicUsage(input_tokens=1, output_tokens=1),
        )
        fake_client = _FakeAnthropicClient(result=fake_result)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_client)

        complete_json("echo_task", PROMPT, Echo, provider="anthropic")

        assert "temperature" not in fake_client.messages.calls[0]

    def test_default_none_omits_temperature_from_openai_call(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="hello"), model="gpt-5", usage=_FakeOpenAIUsage(1, 1)
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        complete_json("echo_task", PROMPT, Echo, provider="openai")

        assert "temperature" not in fake_openai.chat.completions.calls[0]

    def test_explicit_temperature_is_included_in_openai_call(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="hello"), model="gpt-5", usage=_FakeOpenAIUsage(1, 1)
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        complete_json("echo_task", PROMPT, Echo, provider="openai", temperature=0)

        assert fake_openai.chat.completions.calls[0]["temperature"] == 0

    def test_explicit_temperature_is_never_sent_to_anthropic(self, monkeypatch):
        """Anthropic(claude-opus-5)은 temperature를 지원하지 않는다(모듈 docstring) — judge가
        교차 규칙으로 anthropic에 배정돼도 400을 유발하지 않도록 항상 제외해야 한다."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_result = _FakeParsedMessage(
            parsed_output=Echo(value="hello"),
            model="claude-opus-5",
            usage=_FakeAnthropicUsage(input_tokens=1, output_tokens=1),
        )
        fake_client = _FakeAnthropicClient(result=fake_result)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_client)

        complete_json("echo_task", PROMPT, Echo, provider="anthropic", temperature=0)

        assert "temperature" not in fake_client.messages.calls[0]

    def test_explicit_temperature_propagates_through_auto_openai_fallback(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")

        anthropic_error = _anthropic_status_error(anthropic.RateLimitError, 429, "rate limited")
        fake_anthropic = _FakeAnthropicClient(error=anthropic_error)
        monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_anthropic)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="fallback"), model="gpt-5", usage=_FakeOpenAIUsage(1, 1)
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        result = complete_json("echo_task", PROMPT, Echo, temperature=0)

        assert result.provider == "openai"
        assert fake_openai.chat.completions.calls[0]["temperature"] == 0
        # 실패한 anthropic 시도에도 temperature가 실리지 않아야 한다(제약 위반 없음).
        assert "temperature" not in fake_anthropic.messages.calls[0]

    def test_explicit_temperature_propagates_through_auto_no_anthropic_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-test")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        fake_openai_result = _FakeParsedChatCompletion(
            parsed=Echo(value="direct"), model="gpt-5", usage=_FakeOpenAIUsage(1, 1)
        )
        fake_openai = _FakeOpenAIClient(result=fake_openai_result)
        monkeypatch.setattr(openai, "OpenAI", lambda *a, **kw: fake_openai)

        complete_json("echo_task", PROMPT, Echo, temperature=0.3)

        assert fake_openai.chat.completions.calls[0]["temperature"] == 0.3
