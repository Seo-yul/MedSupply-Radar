"""Langfuse tracing 훅(M-22) 테스트 — langfuse SDK 미설치 전제, 전부 모킹.

핵심은 no-op 안전이다: LANGFUSE_* 환경변수가 전부 설정되고 SDK 임포트까지 실제로
성공하지 않는 한, init_tracing()/observed()/record_metadata()는 아무 부작용도 내지
않는다. 이 저장소·CI에는 langfuse가 설치돼 있지 않으므로, 활성 경로
(TestObservedActivePath)만 monkeypatch로 sys.modules에 가짜 langfuse 모듈을 주입해
검증한다 — medsupply.llm.tracing이 SDK를 함수 내부에서 지연 임포트하기 때문에 이
주입만으로 충분하다.

TestWiring은 extract_notice/generate_risk_explanation에 @observed(...)가 실제로
붙어 있는지를 ast로 정적 확인한다. no-op 모드에서는 observed가 원함수를 그대로
반환하므로(브리프: "래핑 오버헤드 0"), 런타임 동작만으로는 데코레이터가 통째로
사라져도 감지할 수 없다 — 그래서 소스를 직접 본다.
"""

from __future__ import annotations

import ast
import inspect
import sys
from types import ModuleType, SimpleNamespace

import pytest

from medsupply.llm import explanation as explanation_module
from medsupply.llm import extraction as extraction_module
from medsupply.llm import tracing
from medsupply.llm.client import LLMResult
from medsupply.llm.explanation import ExplanationResult
from medsupply.llm.extraction import ExtractionResult

_LANGFUSE_ENV_VARS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


@pytest.fixture(autouse=True)
def _clean_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """매 테스트 전에 LANGFUSE_* 세 변수를 확실히 비운다(주변 환경 오염 방지)."""
    for name in _LANGFUSE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_full_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")


# ---------------------------------------------------------------------------
# 가짜 langfuse SDK(활성 경로 전용) — sys.modules에 주입해 실제 SDK 없이 관측 경로를 검증한다.
# ---------------------------------------------------------------------------


class _FakeSpan:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.entered = False
        self.exited = False

    def __enter__(self) -> _FakeSpan:
        self.entered = True
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.exited = True
        return False  # 예외를 삼키지 않는다 — 그대로 전파돼야 한다.

    def update(self, **kwargs) -> None:
        self.updates.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.span_names: list[str] = []

    def start_as_current_span(self, *, name: str) -> _FakeSpan:
        span = _FakeSpan()
        self.spans.append(span)
        self.span_names.append(name)
        return span


def _install_fake_langfuse(monkeypatch: pytest.MonkeyPatch) -> _FakeClient:
    """sys.modules['langfuse']를 가짜 모듈로 치환한다.

    tracing.py는 SDK를 함수 내부에서 지연 임포트하므로(모듈 최상단에 `import langfuse`가
    없으므로), tracing 모듈이 이미 로드된 뒤에도 이 주입만으로 활성 경로에 들어설 수 있다.
    """
    fake_client = _FakeClient()
    fake_module = ModuleType("langfuse")
    fake_module.get_client = lambda: fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langfuse", fake_module)
    return fake_client


# ---------------------------------------------------------------------------
# init_tracing
# ---------------------------------------------------------------------------


class TestInitTracing:
    def test_no_env_vars_returns_false(self) -> None:
        assert tracing.init_tracing() is False

    def test_partial_config_public_key_only_returns_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")

        assert tracing.init_tracing() is False

    def test_progressive_env_and_sdk_availability_without_stale_caching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """멱등이되 캐시하지 않는다 — env·SDK 가용성이 바뀌면 다음 호출이 즉시 반영해야 한다."""
        assert tracing.init_tracing() is False

        _set_full_langfuse_env(monkeypatch)
        # 이 저장소·CI에는 langfuse SDK가 설치돼 있지 않다 — 세 변수를 전부 채워도
        # 임포트가 실패해 계속 no-op이어야 한다(핵심 무부작용 보장).
        assert tracing.init_tracing() is False

        _install_fake_langfuse(monkeypatch)
        assert tracing.init_tracing() is True


# ---------------------------------------------------------------------------
# observed — no-op 모드
# ---------------------------------------------------------------------------


class TestObservedNoOp:
    def test_returns_the_original_function_object_unchanged(self) -> None:
        def sample(x: int, y: int = 1) -> int:
            """샘플 함수."""
            return x + y

        wrapped = tracing.observed("sample_task")(sample)

        assert wrapped is sample  # 래핑 오버헤드 0 — 새 객체를 만들지 않는다.

    def test_decorated_function_behaves_normally(self) -> None:
        @tracing.observed("sample_task")
        def add(x: int, y: int) -> int:
            return x + y

        assert add(2, 3) == 5
        assert add(x=4, y=5) == 9


# ---------------------------------------------------------------------------
# record_metadata
# ---------------------------------------------------------------------------


class TestRecordMetadata:
    def test_extracts_contract_fields_from_extraction_result_shape(self) -> None:
        result = ExtractionResult(
            extraction=None,
            confidence=0.9,
            status="자동확정",
            verification={},
            provider="anthropic",
            model="claude-opus-5",
            prompt_version="notice_extract@v1",
            cache_hit=False,
        )

        metadata = tracing.record_metadata(result)

        assert metadata == {
            "prompt_version": "notice_extract@v1",
            "provider": "anthropic",
            "cache_hit": False,
        }

    def test_extracts_contract_fields_from_explanation_result_shape(self) -> None:
        result = ExplanationResult(
            explanation=None,
            hallucination_flags=("근거 밖 인용",),
            provider="openai",
            model="gpt-5",
            prompt_version="risk_explain@v1",
            cache_hit=True,
        )

        metadata = tracing.record_metadata(result)

        assert metadata == {
            "prompt_version": "risk_explain@v1",
            "provider": "openai",
            "cache_hit": True,
            "hallucination_flags": ("근거 밖 인용",),
        }

    def test_extracts_contract_fields_from_llm_result_shape(self) -> None:
        result = LLMResult(
            data=None,
            provider="anthropic",
            model="claude-opus-5",
            cache_hit=False,
            latency_ms=120,
            trace_id=None,
            usage={"input_tokens": 10, "output_tokens": 20},
        )

        metadata = tracing.record_metadata(result)

        assert metadata == {
            "provider": "anthropic",
            "cache_hit": False,
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

    def test_extracts_nothing_from_plain_object_with_no_contract_fields(self) -> None:
        metadata = tracing.record_metadata(SimpleNamespace(irrelevant="x"))

        assert metadata == {}


# ---------------------------------------------------------------------------
# observed — 활성 모드(가짜 SDK 주입)
# ---------------------------------------------------------------------------


class TestObservedActivePath:
    """LANGFUSE_* 전부 설정 + 가짜 langfuse 모듈이 있을 때만 도달하는 경로."""

    def test_records_span_and_metadata_and_preserves_signature(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_full_langfuse_env(monkeypatch)
        fake_client = _install_fake_langfuse(monkeypatch)

        def fn(x: int, *, y: int = 1):
            """더미 LLM 호출 함수."""
            return SimpleNamespace(provider="anthropic", prompt_version="v1", cache_hit=False)

        wrapped = tracing.observed("dummy_task")(fn)

        assert wrapped is not fn  # 활성 모드에서는 래핑한다.
        assert wrapped.__name__ == fn.__name__
        assert wrapped.__doc__ == fn.__doc__

        result = wrapped(1, y=2)

        assert result.provider == "anthropic"
        assert fake_client.span_names == ["dummy_task"]

        span = fake_client.spans[0]
        assert span.entered is True
        assert span.exited is True
        assert span.updates, "관측 스팬에 최소 1회 update가 기록돼야 한다."
        assert span.updates[-1]["output"] == {
            "provider": "anthropic",
            "prompt_version": "v1",
            "cache_hit": False,
        }

    def test_propagates_exceptions_and_still_closes_span(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_full_langfuse_env(monkeypatch)
        fake_client = _install_fake_langfuse(monkeypatch)

        @tracing.observed("dummy_task")
        def boom():
            raise ValueError("original failure")

        with pytest.raises(ValueError, match="original failure"):
            boom()

        span = fake_client.spans[0]
        assert span.entered is True
        assert span.exited is True


# ---------------------------------------------------------------------------
# 배선 정적 확인 — extraction.py / explanation.py에 @observed(...)가 실제로 붙어 있는지
# ---------------------------------------------------------------------------


def _decorator_task_names(module: ModuleType, func_name: str) -> list[str]:
    """module 소스를 ast로 파싱해 func_name 함수에 붙은 observed(...) 데코레이터의 문자열
    인자를 전부 반환한다."""
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            names = []
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "observed"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    names.append(dec.args[0].value)
            return names
    raise AssertionError(f"{func_name}이(가) {module.__name__}에 없습니다.")


class TestWiring:
    def test_extract_notice_is_wired_with_observed_notice_extract(self) -> None:
        assert _decorator_task_names(extraction_module, "extract_notice") == ["notice_extract"]

    def test_generate_risk_explanation_is_wired_with_observed_risk_explain(self) -> None:
        assert _decorator_task_names(
            explanation_module, "generate_risk_explanation"
        ) == ["risk_explain"]
