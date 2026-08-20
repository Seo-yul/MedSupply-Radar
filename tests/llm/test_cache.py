"""medsupply.llm.cache 테스트(전부 모킹·tmp_path 캐시 파일 — 실제 data/llm_cache.db 미사용).

- TestBuildCacheKey: 캐시 키 결정성·휘발 필드 제거(재귀)·구성요소별 상이성.
- TestCacheRoundTrip: cache_put→cache_get 왕복, init_cache 멱등성, cache_stats.
- TestCompleteJsonCacheIntegration: complete_json의 cache_key/force_refresh 통합
  (offline에서 캐시 히트 우선 포함). tests/llm/conftest.py의 _isolated_llm_cache_path
  오토유즈 픽스처가 settings.LLM_CACHE_PATH를 tmp_path로 돌려놓으므로 실제 파일을
  전혀 건드리지 않는다.
"""

from __future__ import annotations

import anthropic
import pytest
from pydantic import BaseModel

from medsupply.llm import client as llm_client
from medsupply.llm.cache import build_cache_key, cache_get, cache_put, cache_stats, init_cache
from medsupply.llm.client import LLMOfflineError, LLMResult, RenderedPrompt, complete_json

PROMPT = RenderedPrompt(system="system-prompt", user="user-prompt", version="echo@v1")


class Item(BaseModel):
    value: str
    count: int = 0


# --------------------------------------------------------------------------
# TestBuildCacheKey
# --------------------------------------------------------------------------


class TestBuildCacheKey:
    def test_deterministic_regardless_of_payload_key_order(self):
        key_a = build_cache_key("task", "v1", "model-x", Item, {"a": 1, "b": 2})
        key_b = build_cache_key("task", "v1", "model-x", Item, {"b": 2, "a": 1})
        assert key_a == key_b

    def test_strips_volatile_field_at_top_level(self):
        key_with = build_cache_key("task", "v1", "model-x", Item, {"a": 1, "run_id": "x"})
        key_without = build_cache_key("task", "v1", "model-x", Item, {"a": 1})
        assert key_with == key_without

    def test_strips_volatile_field_nested(self):
        key_with = build_cache_key(
            "task", "v1", "model-x", Item, {"a": 1, "meta": {"run_id": "x"}}
        )
        key_without = build_cache_key("task", "v1", "model-x", Item, {"a": 1, "meta": {}})
        assert key_with == key_without

    def test_strips_all_three_volatile_keys_at_any_depth(self):
        payload = {
            "a": 1,
            "run_id": "r",
            "generated_at": "2026-01-01",
            "trace_id": "t",
            "nested": {"run_id": "r2", "generated_at": "g2", "trace_id": "t2", "keep": True},
        }
        cleaned_equivalent = {"a": 1, "nested": {"keep": True}}
        key_dirty = build_cache_key("task", "v1", "model-x", Item, payload)
        key_clean = build_cache_key("task", "v1", "model-x", Item, cleaned_equivalent)
        assert key_dirty == key_clean

    def test_different_task_changes_key(self):
        payload = {"a": 1}
        key_1 = build_cache_key("task_a", "v1", "model-x", Item, payload)
        key_2 = build_cache_key("task_b", "v1", "model-x", Item, payload)
        assert key_1 != key_2

    def test_different_prompt_version_changes_key(self):
        payload = {"a": 1}
        key_1 = build_cache_key("task", "v1", "model-x", Item, payload)
        key_2 = build_cache_key("task", "v2", "model-x", Item, payload)
        assert key_1 != key_2

    def test_different_model_changes_key(self):
        payload = {"a": 1}
        key_1 = build_cache_key("task", "v1", "model-x", Item, payload)
        key_2 = build_cache_key("task", "v1", "model-y", Item, payload)
        assert key_1 != key_2

    def test_different_schema_changes_key(self):
        class Other(BaseModel):
            value: str

        payload = {"a": 1}
        key_1 = build_cache_key("task", "v1", "model-x", Item, payload)
        key_2 = build_cache_key("task", "v1", "model-x", Other, payload)
        assert key_1 != key_2

    def test_returns_hex_sha256_string(self):
        key = build_cache_key("task", "v1", "model-x", Item, {"a": 1})
        assert isinstance(key, str)
        assert len(key) == 64
        int(key, 16)  # 순수 16진수여야 함(ValueError가 나지 않아야 함)


# --------------------------------------------------------------------------
# TestCacheRoundTrip
# --------------------------------------------------------------------------


class TestCacheRoundTrip:
    def test_put_then_get_returns_equivalent_result(self, tmp_path):
        path = tmp_path / "cache.db"
        key = build_cache_key("notice_extract", "v1", "claude-opus-5", Item, {"a": 1})
        original = LLMResult(
            data=Item(value="hello", count=3),
            provider="anthropic",
            model="claude-opus-5",
            cache_hit=False,
            latency_ms=123,
            trace_id="trace-xyz",
            usage={"input_tokens": 10, "output_tokens": 20},
        )

        cache_put(key, "notice_extract", "v1", original, path=path)
        hit = cache_get(key, Item, path=path)

        assert hit is not None
        assert hit.data == original.data  # pydantic 동등
        assert hit.cache_hit is True
        assert hit.provider == original.provider
        assert hit.model == original.model
        assert hit.latency_ms == 0
        assert hit.trace_id is None
        assert hit.usage == original.usage

    def test_get_miss_returns_none(self, tmp_path):
        path = tmp_path / "cache.db"
        assert cache_get("does-not-exist", Item, path=path) is None

    def test_put_overwrites_existing_key(self, tmp_path):
        path = tmp_path / "cache.db"
        key = "fixed-key"
        first = LLMResult(
            data=Item(value="first", count=1),
            provider="anthropic",
            model="claude-opus-5",
            cache_hit=False,
            latency_ms=1,
            trace_id=None,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        second = LLMResult(
            data=Item(value="second", count=2),
            provider="openai",
            model="gpt-5",
            cache_hit=False,
            latency_ms=1,
            trace_id=None,
            usage={"input_tokens": 2, "output_tokens": 2},
        )

        cache_put(key, "task", "v1", first, path=path)
        cache_put(key, "task", "v1", second, path=path)
        hit = cache_get(key, Item, path=path)

        assert hit.data == second.data
        assert hit.provider == "openai"
        assert hit.model == "gpt-5"

    def test_init_cache_is_idempotent(self, tmp_path):
        path = tmp_path / "cache.db"
        init_cache(path=path)
        init_cache(path=path)  # 두 번째 호출도 에러 없이 성공(테이블 이미 존재)
        assert path.exists()

    def test_cache_stats_on_empty_cache(self, tmp_path):
        path = tmp_path / "cache.db"
        assert cache_stats(path=path) == {"entries": 0, "by_task": {}}

    def test_cache_stats_counts_entries_by_task(self, tmp_path):
        path = tmp_path / "cache.db"
        result = LLMResult(
            data=Item(value="v", count=1),
            provider="anthropic",
            model="claude-opus-5",
            cache_hit=False,
            latency_ms=1,
            trace_id=None,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        cache_put("k1", "notice_extract", "v1", result, path=path)
        cache_put("k2", "notice_extract", "v1", result, path=path)
        cache_put("k3", "risk_explain", "v1", result, path=path)

        stats = cache_stats(path=path)

        assert stats["entries"] == 3
        assert stats["by_task"] == {"notice_extract": 2, "risk_explain": 1}


# --------------------------------------------------------------------------
# complete_json 캐시 통합(모킹 클라이언트) — 페이크 Anthropic
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
    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _FakeAnthropicClient:
    def __init__(self, result):
        self.messages = _FakeAnthropicMessages(result)


def _forbidden_anthropic_client(*args, **kwargs):
    raise AssertionError("anthropic.Anthropic()가 호출되어서는 안 된다(캐시 히트/오프라인 경로)")


def _install_fake_anthropic(monkeypatch, value: str, model: str = "claude-opus-5"):
    fake_result = _FakeParsedMessage(
        parsed_output=Item(value=value),
        model=model,
        usage=_FakeAnthropicUsage(input_tokens=1, output_tokens=1),
    )
    fake_client = _FakeAnthropicClient(fake_result)
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **kw: fake_client)
    return fake_client


@pytest.fixture(autouse=True)
def _reset_llm_state(monkeypatch):
    """매 테스트마다 관련 환경변수를 비우고 모듈 수준 클라이언트 캐시를 리셋한다.

    tests/llm/test_client_failover.py의 동명 픽스처와 동일한 목적이며, 파일 간
    결합을 만들지 않기 위해 이 파일에도 독립적으로 둔다.
    """
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


class TestCompleteJsonCacheIntegration:
    def test_online_miss_calls_and_stores_then_second_call_hits_cache(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_client = _install_fake_anthropic(monkeypatch, value="first-call")

        result_1 = complete_json("echo_task", PROMPT, Item, cache_key="int-key-1")
        assert result_1.cache_hit is False
        assert result_1.data == Item(value="first-call")
        assert len(fake_client.messages.calls) == 1

        result_2 = complete_json("echo_task", PROMPT, Item, cache_key="int-key-1")
        assert result_2.cache_hit is True
        assert result_2.data == Item(value="first-call")
        assert result_2.provider == "anthropic"
        assert len(fake_client.messages.calls) == 1  # 재호출 없음(캐시로부터 서빙)

    def test_force_refresh_calls_provider_and_overwrites_cache(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_client = _install_fake_anthropic(monkeypatch, value="v1")

        first = complete_json("echo_task", PROMPT, Item, cache_key="int-key-2")
        assert first.data == Item(value="v1")
        assert len(fake_client.messages.calls) == 1

        fake_client.messages.result = _FakeParsedMessage(
            parsed_output=Item(value="v2"),
            model="claude-opus-5",
            usage=_FakeAnthropicUsage(input_tokens=2, output_tokens=2),
        )

        refreshed = complete_json(
            "echo_task", PROMPT, Item, cache_key="int-key-2", force_refresh=True
        )
        assert refreshed.cache_hit is False
        assert refreshed.data == Item(value="v2")
        assert len(fake_client.messages.calls) == 2  # force_refresh는 캐시를 읽지 않고 호출

        # 캐시가 v2로 덮어써졌는지: force_refresh 없이 다시 부르면 v2가 캐시에서 나와야 함
        third = complete_json("echo_task", PROMPT, Item, cache_key="int-key-2")
        assert third.cache_hit is True
        assert third.data == Item(value="v2")
        assert len(fake_client.messages.calls) == 2  # 추가 호출 없음

    def test_offline_hit_succeeds_without_provider_call(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        fake_client = _install_fake_anthropic(monkeypatch, value="warmed")

        warm = complete_json("echo_task", PROMPT, Item, cache_key="int-key-3")
        assert warm.cache_hit is False
        assert len(fake_client.messages.calls) == 1

        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        result = complete_json("echo_task", PROMPT, Item, cache_key="int-key-3")

        assert result.cache_hit is True
        assert result.data == Item(value="warmed")

    def test_offline_miss_raises_llm_offline_error_with_task_and_key_prefix(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)
        key = "int-key-never-warmed-0123456789"

        with pytest.raises(LLMOfflineError) as exc_info:
            complete_json("echo_task", PROMPT, Item, cache_key=key)

        message = str(exc_info.value)
        assert "echo_task" in message
        assert key[:12] in message

    def test_offline_without_cache_key_raises_existing_error(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        with pytest.raises(LLMOfflineError):
            complete_json("echo_task", PROMPT, Item)

    def test_offline_force_refresh_raises_llm_offline_error(self, monkeypatch):
        monkeypatch.setenv("LLM_MODE", "offline")
        monkeypatch.setattr(anthropic, "Anthropic", _forbidden_anthropic_client)

        with pytest.raises(LLMOfflineError):
            complete_json("echo_task", PROMPT, Item, cache_key="int-key-4", force_refresh=True)
