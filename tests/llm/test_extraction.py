"""공고 구조화 추출(M-13) 테스트 — 전부 모킹, 실 API 불요.

핵심은 환각 통제다: LLM은 NoticeExtraction만 채우고, 신뢰도·확인상태는 전부
medsupply.llm.extraction._verify(순수 함수)가 원문과 발췌를 문자열로 대조해
LLM 밖에서 결정적으로 산정한다. 아래 TestVerify* 클래스들이 그 산식을 직접
검증하고, TestExtractNotice*는 medsupply.llm.extraction 모듈 네임스페이스의
complete_json/load_prompt(둘 다 "from ... import"로 들여온 이름이라 정의 모듈이
아니라 extraction 모듈 쪽을 patch해야 한다)을 페이크로 치환해 LLM 호출 없이
extract_notice()의 조립 로직만 검증한다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from medsupply.llm import extraction as extraction_module
from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import LLMResult, RenderedPrompt
from medsupply.llm.config import load_llm_config
from medsupply.llm.extraction import (
    CONFIDENCE_THRESHOLD,
    ExtractionResult,
    _verify,
    extract_notice,
)
from medsupply.llm.prompts.loader import PromptTemplate
from medsupply.llm.schemas import NoticeExtraction

# --------------------------------------------------------------------------
# 공용 픽스처 데이터
# --------------------------------------------------------------------------

RAW_TEXT = (
    "[공고] 당사는 제조소 설비 점검으로 인해 2026년 7월 20일부터 공급을 중단합니다.\n"
    "재개 예정일은 2026년 9월 15일입니다.\n"
    "품목명: 세프트리악손주 1g. 성분명: 세프트리악손나트륨.\n"
)

QUOTES = [
    "당사는 제조소 설비 점검으로 인해 2026년 7월 20일부터 공급을 중단합니다.",
    "재개 예정일은 2026년 9월 15일입니다.",
    "품목명: 세프트리악손주 1g. 성분명: 세프트리악손나트륨.",
]


def _make_extraction(**overrides) -> NoticeExtraction:
    """기본값은 RAW_TEXT/QUOTES와 완전히 정합하는(감점 없는) 추출 결과다."""
    defaults = dict(
        product_names=["세프트리악손주 1g"],
        ingredient_names=["세프트리악손나트륨"],
        reason="제조소 설비 점검",
        halt_start_date="2026-07-20",
        expected_restart_date="2026-09-15",
        notice_type="공급중단",
        evidence_quotes=list(QUOTES),
    )
    defaults.update(overrides)
    return NoticeExtraction(**defaults)


# --------------------------------------------------------------------------
# TestVerify — 결정적 사후 검증(_verify) 단위 테스트
# --------------------------------------------------------------------------


class TestVerify:
    """confidence 산식·status 결정을 손검산값과 대조한다."""

    def test_all_quotes_found_and_fields_complete_is_auto_confirmed_with_full_confidence(self):
        extraction = _make_extraction()

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert confidence == 1.0
        assert status == "자동확정"
        assert verification == {
            "quotes_total": 3,
            "quotes_found": 3,
            "missing_fields": [],
            "date_parse_ok": True,
            "notes": [],
        }

    def test_one_of_three_missing_quotes_lowers_confidence_by_formula_and_needs_review(self):
        extraction = _make_extraction(
            evidence_quotes=[QUOTES[0], QUOTES[1], "이 문장은 원문 어디에도 존재하지 않는다."]
        )

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        # confidence = 1.0 - 0.5*(1/3) = 0.8333... -> 0.833(소수 3자리)
        assert confidence == 0.833
        # 미발견 quote가 있으므로 confidence와 무관하게 확인 필요
        assert status == "확인 필요"
        assert verification["quotes_total"] == 3
        assert verification["quotes_found"] == 2

    def test_empty_product_names_deducts_confidence_and_needs_review(self):
        # evidence_quotes도 비워, "필수 필드 결손"이 감점의 유일한 원인이 아님을
        # 명확히 한다(평문 발췌 없이 product_names만 비어 있는 시나리오).
        extraction = _make_extraction(product_names=[], evidence_quotes=[])

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        # quotes_total=0 -> 미발견 비율 1.0 취급(-0.5), product_names 결손(-0.2)
        assert confidence == 0.3
        assert status == "확인 필요"
        assert verification["missing_fields"] == ["product_names"]
        assert verification["quotes_total"] == 0
        assert verification["quotes_found"] == 0

    def test_invalid_halt_start_date_sets_date_parse_ok_false_and_deducts_once(self):
        extraction = _make_extraction(halt_start_date="작년 여름")

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert verification["date_parse_ok"] is False
        # 나머지는 전부 정합 -> 날짜 감점 0.1만 적용. confidence 0.9는 임계값(0.8)을
        # 넘지만, date_parse_ok=False 자체가 발췌 불일치와 동급의 결정적 실패 신호라
        # 자동확정하지 않는다(픽스 라운드 1 — 컨트롤러 판정).
        assert confidence == 0.9
        assert status == "확인 필요"

    def test_both_invalid_dates_still_deduct_only_once(self):
        extraction = _make_extraction(
            halt_start_date="작년 여름", expected_restart_date="내년 봄"
        )

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert verification["date_parse_ok"] is False
        # 날짜 필드 2개가 모두 실패해도 감점은 0.1 한 번뿐이다.
        assert confidence == 0.9
        assert status == "확인 필요"

    def test_null_dates_do_not_fail_parsing(self):
        extraction = _make_extraction(halt_start_date=None, expected_restart_date=None)

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert verification["date_parse_ok"] is True
        assert confidence == 1.0

    def test_whitespace_normalized_quote_matches_across_newlines_and_multiple_spaces(self):
        raw_text_multiline = (
            "제조소 사정으로\n2026년   7월      20일부터\n공급이  중단됩니다."
        )
        extraction = _make_extraction(
            evidence_quotes=["제조소 사정으로 2026년 7월 20일부터 공급이 중단됩니다."]
        )

        confidence, status, verification = _verify(extraction, raw_text_multiline)

        assert verification["quotes_total"] == 1
        assert verification["quotes_found"] == 1
        assert confidence == 1.0
        assert status == "자동확정"

    def test_missing_reason_and_invalid_notice_type_are_both_listed_and_deducted(self):
        extraction = _make_extraction(reason="", notice_type="알수없음")

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert verification["missing_fields"] == ["reason", "notice_type"]
        assert confidence == 0.6
        assert status == "확인 필요"

    def test_whitespace_only_reason_counts_as_missing(self):
        extraction = _make_extraction(reason="   ")

        _, _, verification = _verify(extraction, RAW_TEXT)

        assert "reason" in verification["missing_fields"]

    def test_empty_ingredient_names_does_not_deduct_confidence(self):
        """ingredient_names는 브리프의 '필수 필드' 3종(product_names/reason/notice_type)에
        포함되지 않으므로 비어 있어도 감점되지 않는다."""
        extraction = _make_extraction(ingredient_names=[])

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert confidence == 1.0
        assert status == "자동확정"
        assert verification["missing_fields"] == []

    def test_confidence_is_clamped_at_zero_lower_bound(self):
        extraction = _make_extraction(
            product_names=[],
            reason="",
            notice_type="알수없음",
            halt_start_date="작년 여름",
            evidence_quotes=[],
        )

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        # 이론상 1.0-0.5-0.6-0.1 = -0.2 이지만 하한 0.0으로 클램프.
        assert confidence == 0.0
        assert status == "확인 필요"

    def test_never_returns_review_complete_status(self):
        """'확인 완료'는 사람 액션 전용 — _verify는 절대 반환하지 않는다."""
        _, status_high, _ = _verify(_make_extraction(), RAW_TEXT)
        _, status_low, _ = _verify(
            _make_extraction(product_names=[], evidence_quotes=[]), RAW_TEXT
        )

        assert status_high != "확인 완료"
        assert status_low != "확인 완료"
        assert {status_high, status_low} <= {"자동확정", "확인 필요"}


# --------------------------------------------------------------------------
# TestAutoConfirmGateRequiresAllDeterministicSignals — 픽스 라운드 1 회귀 테스트
#
# 태스크 리뷰 컨트롤러 판정: 필수 필드 결손·날짜 파싱 실패는 confidence 감점으로
# 희석될 신호가 아니라 발췌 불일치와 동급의 결정적 실패 신호다(보수적 기본값
# 철학). confidence 산식 자체는 불변이며, 자동확정 게이트에
# `not missing_fields and date_parse_ok`가 추가됐다.
# --------------------------------------------------------------------------


class TestAutoConfirmGateRequiresAllDeterministicSignals:
    def test_single_missing_field_with_full_quotes_is_not_auto_confirmed(self):
        """필드 결손 1건 + 발췌 전부 매칭이면 confidence가 정확히 임계값(0.8)에
        도달하지만, missing_fields가 비어있지 않으므로 자동확정하지 않는다 —
        게이트 수정 전에는 이 경계에서 잘못 자동확정됐다(원 리포트에서 플래그한
        경계 케이스)."""
        extraction = _make_extraction(product_names=[])  # QUOTES 3개는 그대로 전부 매칭

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert confidence == 0.8
        assert verification["missing_fields"] == ["product_names"]
        assert verification["quotes_found"] == verification["quotes_total"]
        assert status == "확인 필요"

    def test_date_parse_failure_alone_is_not_auto_confirmed(self):
        """날짜 파싱 실패 하나만 있어도(그 외 전부 완전, confidence 0.9) 자동확정하지
        않는다 — confidence가 임계값을 넘어도 date_parse_ok=False면 결정적으로
        확인 필요로 강등된다."""
        extraction = _make_extraction(expected_restart_date="내년 봄")

        confidence, status, verification = _verify(extraction, RAW_TEXT)

        assert confidence == 0.9
        assert verification["date_parse_ok"] is False
        assert verification["missing_fields"] == []
        assert status == "확인 필요"


# --------------------------------------------------------------------------
# TestVerifyAgainstRealNoticeSample — 실제 원문(data/notices/raw/001)
# --------------------------------------------------------------------------


class TestVerifyAgainstRealNoticeSample:
    """LLM 없이, 실제 공고 원문 파일로 발췌-원문 대조 판별을 검증한다."""

    RAW_PATH = (
        Path(__file__).resolve().parent.parent.parent
        / "data"
        / "notices"
        / "raw"
        / "001_2024-10-17_아지트로마이신_아지탑스주사_공급부족.txt"
    )

    @staticmethod
    def _raw_text(path: Path) -> str:
        """scripts/load_notices.py와 동일한 파싱 규약: 첫 3줄(헤더)을 제외한 본문."""
        text = path.read_text(encoding="utf-8")
        return text.split("\n", 3)[3]

    def test_existing_quote_is_found_and_missing_quote_is_not(self):
        raw_text = self._raw_text(self.RAW_PATH)
        assert "판매증가로 인한 품절 발생" in raw_text  # 사전 확인: 원문에 실존

        extraction = NoticeExtraction(
            product_names=["아지탑스주사500밀리그램(아지트로마이신수화물)"],
            ingredient_names=["아지트로마이신수화물"],
            reason="판매증가로 인한 품절 발생",
            halt_start_date=None,
            expected_restart_date="2024-11-05",
            notice_type="공급부족",
            evidence_quotes=[
                "판매증가로 인한 품절 발생",
                "이 문장은 원문 어디에도 존재하지 않는 가상의 발췌입니다.",
            ],
        )

        confidence, status, verification = _verify(extraction, raw_text)

        assert verification["quotes_total"] == 2
        assert verification["quotes_found"] == 1
        assert status == "확인 필요"


# --------------------------------------------------------------------------
# 페이크 complete_json / LLMResult 헬퍼
# --------------------------------------------------------------------------


def _fake_llm_result(
    data: NoticeExtraction, *, provider: str = "anthropic", model: str = "claude-opus-5", cache_hit: bool = False
) -> LLMResult:
    return LLMResult(
        data=data,
        provider=provider,
        model=model,
        cache_hit=cache_hit,
        latency_ms=0,
        trace_id=None,
        usage={"input_tokens": 1, "output_tokens": 1},
    )


class _FakeCompleteJson:
    """medsupply.llm.extraction.complete_json 자리를 대신하는 콜 기록용 페이크."""

    def __init__(self, result: LLMResult):
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, task, prompt, schema, **kwargs):
        self.calls.append({"task": task, "prompt": prompt, "schema": schema, **kwargs})
        return self._result


# --------------------------------------------------------------------------
# TestExtractNotice — extract_notice() 통합(complete_json 모킹)
# --------------------------------------------------------------------------


class TestExtractNotice:
    def test_cache_key_is_deterministic_and_passed_to_complete_json(self, monkeypatch):
        raw_text = "원문 샘플입니다."
        fake = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake)

        extract_notice(raw_text)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["task"] == "notice_extract"
        assert call["schema"] is NoticeExtraction
        assert isinstance(call["prompt"], RenderedPrompt)
        assert call["force_refresh"] is False

        cfg = load_llm_config()
        expected_key = build_cache_key(
            "notice_extract", "v1", cfg.anthropic_model, NoticeExtraction, {"raw_text": raw_text}
        )
        assert call["cache_key"] == expected_key

    def test_same_raw_text_always_yields_same_cache_key(self, monkeypatch):
        raw_text = "동일한 원문."
        fake_a = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_a)
        extract_notice(raw_text)

        fake_b = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_b)
        extract_notice(raw_text)

        assert fake_a.calls[0]["cache_key"] == fake_b.calls[0]["cache_key"]

    def test_different_raw_text_yields_different_cache_key(self, monkeypatch):
        fake_a = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_a)
        extract_notice("원문 A")

        fake_b = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_b)
        extract_notice("원문 B")

        assert fake_a.calls[0]["cache_key"] != fake_b.calls[0]["cache_key"]

    def test_force_refresh_is_propagated_to_complete_json(self, monkeypatch):
        fake = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake)

        extract_notice("원문 샘플입니다.", force_refresh=True)

        assert fake.calls[0]["force_refresh"] is True

    def test_notice_id_is_accepted_but_does_not_affect_cache_key(self, monkeypatch):
        raw_text = "원문 샘플입니다."

        fake_a = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_a)
        extract_notice(raw_text, notice_id="N-001")

        fake_b = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_b)
        extract_notice(raw_text, notice_id="N-999")

        assert fake_a.calls[0]["cache_key"] == fake_b.calls[0]["cache_key"]

    def test_prompt_version_override_is_rendered_and_reflected_in_cache_key(self, monkeypatch):
        raw_text = "원문 샘플입니다."
        fake_template = PromptTemplate(
            task="notice_extract",
            version="v2-test",
            system="너는 추출 도우미다.",
            user_template="원문: {raw_text}",
        )

        captured_versions: list[str | None] = []

        def fake_load_prompt(task, version=None):
            assert task == "notice_extract"
            captured_versions.append(version)
            return fake_template

        monkeypatch.setattr(extraction_module, "load_prompt", fake_load_prompt)

        fake_complete = _FakeCompleteJson(_fake_llm_result(_make_extraction()))
        monkeypatch.setattr(extraction_module, "complete_json", fake_complete)

        result = extract_notice(raw_text, prompt_version="v2-test")

        assert captured_versions == ["v2-test"]
        assert result.prompt_version == "v2-test"

        call = fake_complete.calls[0]
        assert call["prompt"].version == "v2-test"
        assert call["prompt"].user == "원문: 원문 샘플입니다."

        cfg = load_llm_config()
        expected_key = build_cache_key(
            "notice_extract", "v2-test", cfg.anthropic_model, NoticeExtraction, {"raw_text": raw_text}
        )
        assert call["cache_key"] == expected_key

    def test_return_fields_are_assembled_from_llm_result_and_verification(self, monkeypatch):
        raw_text = "당사는 사정으로 공급을 중단합니다."
        extraction = NoticeExtraction(
            product_names=["품목A"],
            ingredient_names=["성분A"],
            reason="사정",
            halt_start_date=None,
            expected_restart_date=None,
            notice_type="공급중단",
            evidence_quotes=["당사는 사정으로 공급을 중단합니다."],
        )
        fake_result = _fake_llm_result(extraction, provider="openai", model="gpt-5", cache_hit=True)
        monkeypatch.setattr(extraction_module, "complete_json", _FakeCompleteJson(fake_result))

        result = extract_notice(raw_text)

        assert isinstance(result, ExtractionResult)
        assert result.extraction == extraction
        assert result.provider == "openai"
        assert result.model == "gpt-5"
        assert result.cache_hit is True
        assert result.prompt_version == "v1"
        assert result.confidence == 1.0
        assert result.status == "자동확정"
        assert result.verification["quotes_total"] == 1
        assert result.verification["quotes_found"] == 1


# --------------------------------------------------------------------------
# 모듈 상수
# --------------------------------------------------------------------------


def test_confidence_threshold_constant_is_zero_point_eight():
    assert CONFIDENCE_THRESHOLD == 0.8


# --------------------------------------------------------------------------
# (선택) 실 API 스모크 — 키가 없으면 CI 안전하게 skip
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="ANTHROPIC_API_KEY/OPENAI_API_KEY가 없으면 실제 LLM 추출 스모크를 건너뛴다",
)
def test_smoke_real_extraction_on_sample_001():
    raw_text = TestVerifyAgainstRealNoticeSample._raw_text(
        TestVerifyAgainstRealNoticeSample.RAW_PATH
    )

    result = extract_notice(raw_text, force_refresh=True)

    assert isinstance(result.extraction, NoticeExtraction)
    assert result.status in ("자동확정", "확인 필요")
    assert 0.0 <= result.confidence <= 1.0
    assert result.provider in ("anthropic", "openai")
