"""Task X-1: 평가 페이지 "LLM 사용량·추정 비용" 카드 테스트.

settings.LLM_CACHE_PATH(medsupply/llm/cache.py의 llm_cache 테이블)를 task×model_used로
집계하는 evaluation_service.load_llm_usage()와, 그 결과를 렌더하는
views/evaluation.py의 LLM 사용량 카드를 검증한다.

현재 이 캐시 DB는 저장소에 존재하지 않는다(LLM 키 미설정) — "부재가 기본 경로"이므로
그 경로를 가장 두텁게 검증한다. 픽스처 캐시 DB는 medsupply.llm.cache의 실함수
(init_cache·cache_put)로만 만든다 — 테스트가 직접 INSERT를 실행하면 llm_cache 스키마가
바뀌었을 때도 테스트가 조용히 드리프트할 수 있기 때문이다(브리프 지시).

격리: services.evaluation.LLM_CACHE_PATH를 tmp_path로 monkeypatch한다(표준 DB인
data/llm_cache.db를 절대 만들거나 건드리지 않는다 — REPORT_PATHS 등과 동일한 이 모듈의
기존 격리 관례). AppTest 렌더 테스트는 REPORT_PATHS·EVAL_CONFIG_PATH·EVAL_RESULTS_DIR도
존재하지 않는 tmp 경로로 격리해, 저장소의 실제 reports/ 내용이 이 카드와 무관한 문자열
검증에 우연히 섞이지 않게 한다(test_evaluation_live.py의 _empty_report_tree/_activate와
동일한 기법을 이 파일에서 자체적으로 재현한다 — LLM_CACHE_PATH 격리와 한 곳에서 함께
관리하기 위해 import 대신 로컬 헬퍼로 둔다).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import streamlit as st
from pydantic import BaseModel
from streamlit.testing.v1 import AppTest

from medsupply.llm.cache import cache_put, init_cache
from medsupply.llm.client import LLMResult
from medsupply.services import evaluation as evaluation_service

# ---------------------------------------------------------------------------
# 픽스처 캐시 DB 빌더 — cache.py의 실함수만 사용(스키마 드리프트 방지, 브리프 지시).
# ---------------------------------------------------------------------------


class _FixturePayload(BaseModel):
    """cache_put이 요구하는 pydantic BaseModel data — 이 카드가 신경 쓰는 것은
    provider/model/usage_json뿐이라 내용은 의미 없는 placeholder다."""

    note: str = "fixture"


def _seed_cache_row(
    path: Path,
    *,
    key: str,
    task: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    provider: str = "anthropic",
    prompt_version: str = "v1",
) -> None:
    init_cache(path=path)
    result = LLMResult(
        data=_FixturePayload(),
        provider=provider,
        model=model,
        cache_hit=False,
        latency_ms=1,
        trace_id=None,
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens},
    )
    cache_put(key, task, prompt_version, result, path=path)


# ---------------------------------------------------------------------------
# 서비스 계층 단위 검증 — AppTest 없이 evaluation_service.load_llm_usage 직접 호출
# ---------------------------------------------------------------------------


class TestLoadLlmUsageAbsent:
    def test_missing_db_returns_available_false_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", tmp_path / "nowhere" / "llm_cache.db")

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        assert result == {"available": False}

    def test_missing_db_is_not_created_as_a_side_effect(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """조회만으로 캐시 DB 파일이 생기면 "부재가 기본"이라는 브리프 원칙이 깨진다 —
        load_llm_usage는 init_cache를 호출해서는 안 된다."""
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)

        evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        assert not cache_path.exists()


class TestLoadLlmUsageCorrupted:
    def test_corrupted_db_is_isolated_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_path = tmp_path / "llm_cache.db"
        cache_path.write_bytes(b"not a sqlite database at all, just garbage bytes 1234567890")
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        assert result["available"] is False
        assert "error" in result
        assert "rows" not in result


class TestLoadLlmUsageAggregation:
    def test_single_opus_row_cost_hand_calculated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """브리프 §산출물의 손검산 예시: opus 입력 30,000·출력 10,000 → $0.40
        ((30000/1e6)*5.0 + (10000/1e6)*25.0 = 0.15 + 0.25)."""
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        _seed_cache_row(
            cache_path, key="k1", task="notice_extract", model="claude-opus-5",
            input_tokens=30_000, output_tokens=10_000,
        )

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        assert result["available"] is True
        assert len(result["rows"]) == 1
        row = result["rows"][0]
        assert row == {
            "task": "notice_extract", "model": "claude-opus-5", "calls": 1,
            "in_tokens": 30_000, "out_tokens": 10_000, "est_cost_usd": pytest.approx(0.40),
        }
        assert result["totals"]["est_cost_usd"] == pytest.approx(0.40)
        assert result["generated_basis"] == "누적 호출 1건"

    def test_unknown_model_cost_is_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        _seed_cache_row(
            cache_path, key="k1", task="judge", model="totally-unknown-model",
            input_tokens=1_000, output_tokens=1_000,
        )

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        row = result["rows"][0]
        assert row["est_cost_usd"] is None
        # 단가를 모르는 행만 있으면 합계도 추측하지 않는다.
        assert result["totals"]["est_cost_usd"] is None

    def test_snapshot_id_variant_falls_back_to_prefix_price(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        _seed_cache_row(
            cache_path, key="k1", task="judge", model="claude-opus-5-20260115",
            input_tokens=1_000_000, output_tokens=0,
        )

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        row = result["rows"][0]
        assert row["model"] == "claude-opus-5-20260115"  # 원본 스냅샷 ID는 그대로 보존
        assert row["est_cost_usd"] == pytest.approx(5.0)  # 1M 입력 토큰 * $5.0/mtok

    def test_groups_by_task_and_model(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        _seed_cache_row(
            cache_path, key="k1", task="judge", model="claude-opus-5",
            input_tokens=1_000, output_tokens=1_000,
        )
        _seed_cache_row(
            cache_path, key="k2", task="judge", model="claude-opus-5",
            input_tokens=2_000, output_tokens=2_000,
        )
        _seed_cache_row(
            cache_path, key="k3", task="judge", model="gpt-5",
            input_tokens=500, output_tokens=500,
        )
        _seed_cache_row(
            cache_path, key="k4", task="notice_extract", model="claude-opus-5",
            input_tokens=100, output_tokens=100,
        )

        result = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())

        assert len(result["rows"]) == 3  # (judge,opus) (judge,gpt-5) (notice_extract,opus)
        judge_opus = next(
            r for r in result["rows"] if r["task"] == "judge" and r["model"] == "claude-opus-5"
        )
        assert judge_opus["calls"] == 2
        assert judge_opus["in_tokens"] == 3_000
        assert judge_opus["out_tokens"] == 3_000
        assert result["totals"]["calls"] == 4
        assert result["generated_basis"] == "누적 호출 4건"


class TestLoadLlmUsageMtimeInvalidation:
    def test_touching_cache_db_changes_mtimes_tuple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        init_cache(path=cache_path)

        before = evaluation_service.current_report_mtimes()

        stat = cache_path.stat()
        os.utime(cache_path, (stat.st_atime + 5, stat.st_mtime + 5))

        after = evaluation_service.current_report_mtimes()
        assert before != after

    def test_load_llm_usage_cache_reflects_new_mtime_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """캐시 clear 관례 없이도(mtimes 인자가 바뀌면) 새로 쓴 행이 반영돼야 한다 —
        load_eval_reports의 동일 계약(test_evaluation_live.py)을 load_llm_usage에도
        고정한다."""
        cache_path = tmp_path / "llm_cache.db"
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        st.cache_data.clear()

        _seed_cache_row(
            cache_path, key="k1", task="judge", model="claude-opus-5",
            input_tokens=1_000, output_tokens=1_000,
        )
        first = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())
        assert first["totals"]["calls"] == 1

        _seed_cache_row(
            cache_path, key="k2", task="judge", model="claude-opus-5",
            input_tokens=1_000, output_tokens=1_000,
        )
        stat = cache_path.stat()
        os.utime(cache_path, (stat.st_atime + 5, stat.st_mtime + 5))

        second = evaluation_service.load_llm_usage(evaluation_service.current_report_mtimes())
        assert second["totals"]["calls"] == 2


# ---------------------------------------------------------------------------
# 렌더 계층 — AppTest 2경로(카드 표시 / 기록 없음)
# ---------------------------------------------------------------------------


def _isolate_other_report_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """이 카드는 LLM 캐시 DB만 조회하지만 render()는 여전히 load_eval_reports도 호출한다
    — 저장소의 실제 reports/ 내용이 이 카드의 문자열 검증과 우연히 섞이지 않도록 나머지
    리포트 경로도 존재하지 않는 tmp 경로로 격리한다(모든 다른 섹션은 "실측 전"으로
    렌더되어 이 카드의 검증과 무관해진다)."""
    empty_paths = {key: tmp_path / "reports_absent" / f"{key}.json" for key in evaluation_service.REPORT_PATHS}
    monkeypatch.setattr(evaluation_service, "REPORT_PATHS", empty_paths)
    monkeypatch.setattr(evaluation_service, "EVAL_CONFIG_PATH", tmp_path / "eval_absent" / "config.yaml")
    monkeypatch.setattr(evaluation_service, "EVAL_RESULTS_DIR", tmp_path / "eval_absent" / "results")


def _run_evaluation_page() -> None:
    from medsupply import theme
    from medsupply.views import evaluation

    theme.inject_css()
    evaluation.render()


def _rendered_text(at: AppTest) -> str:
    return "\n".join(md.value for md in at.markdown)


class TestLlmUsageCardRenderWithData:
    @pytest.fixture()
    def rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        cache_path = tmp_path / "llm_cache.db"
        _seed_cache_row(
            cache_path, key="k1", task="notice_extract", model="claude-opus-5",
            input_tokens=30_000, output_tokens=10_000,
        )
        _seed_cache_row(
            cache_path, key="k2", task="risk_explain", model="unknown-model-x1",
            input_tokens=1_000, output_tokens=500,
        )
        _seed_cache_row(
            cache_path, key="k3", task="mystery_task", model="gpt-5-2026-08-01",
            input_tokens=2_000_000, output_tokens=1_000_000,
        )
        _isolate_other_report_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        st.cache_data.clear()

        at = AppTest.from_function(_run_evaluation_page)
        at.run()
        assert not at.exception
        return _rendered_text(at)

    def test_task_labels_localized_and_unknown_kept_raw(self, rendered: str) -> None:
        assert "공고 추출" in rendered  # notice_extract
        assert "원인 설명" in rendered  # risk_explain
        assert "mystery_task" in rendered  # 미지 값은 원문 그대로

    def test_model_calls_and_cost_per_row_shown(self, rendered: str) -> None:
        assert "claude-opus-5" in rendered
        assert "$0.40" in rendered  # 브리프 손검산 케이스
        assert "$12.50" in rendered  # 접두 폴백(gpt-5-2026-08-01 → gpt-5): 2*1.25+1*10
        assert "-" in rendered  # 미지 모델(unknown-model-x1) 비용은 '-'

    def test_totals_row_present_and_summed(self, rendered: str) -> None:
        assert "합계" in rendered
        assert "$12.90" in rendered  # 0.40 + 12.50(단가를 아는 행만 합산)

    def test_caption_has_exact_disclaimer_text_and_basis(self, rendered: str) -> None:
        assert (
            "추정치 — 단가 2026-08 기준, 캐시 적중 재사용은 과금·집계 제외."
            " 실청구는 프로바이더 콘솔 참조."
        ) in rendered
        assert "누적 호출 3건" in rendered

    def test_no_pending_badge_leaks_into_this_card(self, rendered: str) -> None:
        assert "LLM 호출 기록 없음" not in rendered


class TestLlmUsageCardRenderAbsent:
    @pytest.fixture()
    def rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        cache_path = tmp_path / "nowhere" / "llm_cache.db"  # 생성하지 않는다(부재 경로)
        _isolate_other_report_paths(monkeypatch, tmp_path)
        monkeypatch.setattr(evaluation_service, "LLM_CACHE_PATH", cache_path)
        st.cache_data.clear()

        at = AppTest.from_function(_run_evaluation_page)
        at.run()
        assert not at.exception
        return _rendered_text(at)

    def test_no_record_message_shown(self, rendered: str) -> None:
        assert "LLM 호출 기록 없음 — 키 설정 후 파이프라인 실행 시 집계된다." in rendered

    def test_no_cost_table_or_caption_leaks_through(self, rendered: str) -> None:
        assert "추정치 — 단가 2026-08 기준" not in rendered
        assert "합계" not in rendered
