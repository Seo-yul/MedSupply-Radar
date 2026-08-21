"""Task S-31: views/evaluation.py 실측 리포트 연동 렌더 테스트(AppTest).

tests/platform/test_alerts_live.py·test_situation_live.py와 동일 관례 — AppTest로 실제
render()를 구동하고 렌더된 마크다운 텍스트를 검사한다. 다만 이 뷰는 DB가 아니라
저장소 내 리포트 **파일**을 읽으므로, 표준 스냅샷 대신 tmp_path에 합성 최소 JSON(실제
reports/ 파일의 복사본이 아니다)을 만들고 medsupply.services.evaluation의 경로 상수
(REPORT_PATHS·EVAL_RESULTS_DIR·EVAL_CONFIG_PATH)를 그 tmp 트리로 monkeypatch한다 —
settings.DB_PATH를 monkeypatch하는 다른 _live 테스트들과 같은 격리 기법을 파일 계층에
적용한 것이다. st.cache_data는 프로세스 전역이라 매 테스트에서 반드시 clear()한다.

세 경로를 검증한다(브리프 §산출물 그대로).
- TestAllReportsPresent: 리포트 전부 있는 tmp 픽스처로 실제 값이 렌더되는지(감지 성능의
  raw·지평 내 병기 + 경고+ 행 + 블라인드 1·2차 + 메커니즘 문구, 수요예측 음수 개선율,
  플랫폼 검증 E2E/perf/재현성 3계열, LLM 평가 추출 정확도/judge 요약/judge 구성).
- TestAllReportsAbsent: 리포트 전부 없는 경로 — 전 블록이 "실측 전"으로 렌더되는지.
- TestOneCorruptedReport: detection_metrics.json 1개만 깨진 JSON — 그 블록만 "실측 전"
  으로 격리되고 나머지 블록(별도 파일인 블라인드 요약 포함)은 정상 렌더되는지.

TestLoadEvalReports: AppTest 없이 evaluation_service.load_eval_reports를 직접 호출해
반환 dict 형태(exists/data/error 키)를 손검산한다(렌더 계층과 분리된 빠른 단위 검증).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply.services import evaluation as evaluation_service

# ---------------------------------------------------------------------------
# 합성 최소 JSON 픽스처 데이터(실제 reports/ 파일의 복사본이 아니다 — 뷰가 실제로
# 읽는 키만 담은 최소 구성)
# ---------------------------------------------------------------------------

_DETECTION_METRICS = {
    "meta": {"config_hash": "testhash1"},
    "results": {
        "detection_rate": 0.9,
        "false_positive_rate": 0.45,
        "lead_days": {"median": 26.5},
        "within_horizon": {
            "threshold_watch": {"detection_rate": 0.93},
            "threshold_warning": {"detection_rate": 0.67, "false_positive_rate": 0.04},
        },
        "threshold_warning": {
            "detection_rate": 0.65, "false_positive_rate": 0.04, "lead_days": {"median": 10},
        },
    },
    "calibration": {"adopted": "cand-F"},
}
_BLIND_SUMMARY = {"aggregate": {"detection_rate": {"mean": 0.35}, "false_positive_rate": {"mean": 0.555}}}
_BLIND_ROUND2_SUMMARY = {
    "aggregate": {"detection_rate": {"mean": 1.0}, "false_positive_rate": {"mean": 0.4467}}
}
_FORECAST_MAPE = {
    "overall": {
        "ses_mape_mean": 0.3205, "sma_mape_mean": 0.3125,
        "baseline_improved": -0.0256, "ses_win_rate": 0.3306,
    }
}
_E2E_RESULTS = {"passed_runs": 10, "runs": 10, "verdict": True}
_PERF_RESULTS = {
    "targets": {"assess_snapshot": {"p95_ms": 416.5}, "list_items": {"p95_ms": 0.7}},
    "verdict": True,
}
_REPRODUCIBILITY = {
    "generation": {"identical": True}, "batch": {"identical": True},
    "detection": {"identical": True}, "verdict": True,
}
_EXTRACTION_ACCURACY = {"macro_accuracy": 0.85}
_EVAL_CONFIG_YAML = """\
rubric_version: v1
judge_by_generation_provider:
  anthropic: {provider: openai, model: gpt-5}
  openai: {provider: anthropic, model: claude-opus-5}
"""


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _build_full_report_tree(root: Path) -> dict[str, Path]:
    """REPORT_PATHS와 동일한 키로 tmp 경로 dict를 만들고, 그 경로에 합성 데이터를
    전부 써 넣는다(eval/config.yaml·eval/results/도 함께)."""
    paths = {
        "detection_metrics": root / "reports" / "analytics" / "detection_metrics.json",
        "blind_summary": root / "reports" / "analytics" / "blind_summary.json",
        "blind_round2_summary": root / "reports" / "analytics" / "blind_round2_summary.json",
        "forecast_mape": root / "reports" / "analytics" / "forecast_mape.json",
        "e2e_results": root / "reports" / "platform" / "e2e_results.json",
        "perf_results": root / "reports" / "platform" / "perf_results.json",
        "reproducibility": root / "reports" / "platform" / "reproducibility.json",
        "extraction_accuracy": root / "reports" / "llm" / "extraction_accuracy.json",
    }
    _write_json(paths["detection_metrics"], _DETECTION_METRICS)
    _write_json(paths["blind_summary"], _BLIND_SUMMARY)
    _write_json(paths["blind_round2_summary"], _BLIND_ROUND2_SUMMARY)
    _write_json(paths["forecast_mape"], _FORECAST_MAPE)
    _write_json(paths["e2e_results"], _E2E_RESULTS)
    _write_json(paths["perf_results"], _PERF_RESULTS)
    _write_json(paths["reproducibility"], _REPRODUCIBILITY)
    _write_json(paths["extraction_accuracy"], _EXTRACTION_ACCURACY)

    eval_config_path = root / "eval" / "config.yaml"
    eval_config_path.parent.mkdir(parents=True, exist_ok=True)
    eval_config_path.write_text(_EVAL_CONFIG_YAML, encoding="utf-8")

    eval_results_dir = root / "eval" / "results"
    eval_results_dir.mkdir(parents=True, exist_ok=True)
    _write_json(eval_results_dir / "exp_20260101_120000.json", {"cases": 4})

    return paths


def _empty_report_tree(root: Path) -> dict[str, Path]:
    """파일을 하나도 만들지 않은 빈 경로 dict(전부 부재 시나리오)."""
    return {
        key: root / "reports" / "nowhere" / f"{key}.json"
        for key in (
            "detection_metrics", "blind_summary", "blind_round2_summary", "forecast_mape",
            "e2e_results", "perf_results", "reproducibility", "extraction_accuracy",
        )
    }


def _activate(monkeypatch: pytest.MonkeyPatch, root: Path, *, report_paths: dict[str, Path]) -> None:
    monkeypatch.setattr(evaluation_service, "REPORT_PATHS", report_paths)
    monkeypatch.setattr(evaluation_service, "EVAL_CONFIG_PATH", root / "eval" / "config.yaml")
    monkeypatch.setattr(evaluation_service, "EVAL_RESULTS_DIR", root / "eval" / "results")
    st.cache_data.clear()


def _run_evaluation_page() -> None:
    from medsupply import theme
    from medsupply.views import evaluation

    theme.inject_css()
    evaluation.render()


def _rendered_text(at: AppTest) -> str:
    return "\n".join(md.value for md in at.markdown)


# ---------------------------------------------------------------------------
# 전부 있음 — 실제 값 렌더
# ---------------------------------------------------------------------------


class TestAllReportsPresent:
    @pytest.fixture()
    def rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        report_paths = _build_full_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        at = AppTest.from_function(_run_evaluation_page)
        at.run()
        assert not at.exception
        return _rendered_text(at)

    def test_detection_section_shows_raw_and_within_horizon_paired(self, rendered: str) -> None:
        assert "감지 성능" in rendered
        assert "90.0%" in rendered  # raw 주의+ 감지율
        assert "93.0%" in rendered  # 지평 내 주의+ 감지율
        assert "45.0%" in rendered  # 오탐률(주의+)
        assert "26.5일" in rendered  # 선행 중앙값(주의+)

    def test_detection_section_shows_warning_plus_row(self, rendered: str) -> None:
        assert "65.0%" in rendered  # raw 경고+ 감지율
        assert "67.0%" in rendered  # 지평 내 경고+ 감지율

    def test_detection_caption_has_params_hash_and_notice_condition(self, rendered: str) -> None:
        assert "testhash1" in rendered
        assert "공고 추출 미반영" in rendered

    def test_calibration_adopted_candidate_shown(self, rendered: str) -> None:
        assert "cand-F" in rendered

    def test_blind_summary_shows_both_rounds_and_mechanism_phrase(self, rendered: str) -> None:
        assert "35.0%" in rendered  # 블라인드 1차 감지
        assert "55.5%" in rendered  # 블라인드 1차 오탐
        assert "100.0%" in rendered  # 블라인드 2차 감지
        assert "44.67%" in rendered  # 블라인드 2차 오탐(like-for-like)
        # 사용자 지시: 수치만 단독 노출 금지 — "탐지기 동일·라벨 배치 수정" 메커니즘
        # 문구가 반드시 함께 붙어야 한다.
        assert "탐지기 동일" in rendered
        assert "라벨 배치 수정" in rendered

    def test_forecast_section_shows_negative_improvement_as_is(self, rendered: str) -> None:
        assert "수요예측" in rendered
        assert "0.3205" in rendered
        assert "0.3125" in rendered
        assert "-2.56%" in rendered  # 음수 개선율도 그대로(통과선 문구 없이)
        assert "33.1%" in rendered  # SES 승률

    def test_forecast_section_has_no_pass_bar_language(self, rendered: str) -> None:
        assert "목표 달성" not in rendered
        assert "PASS" not in rendered

    def test_platform_section_shows_e2e_perf_and_reproducibility(self, rendered: str) -> None:
        assert "플랫폼 검증" in rendered
        assert "10/10" in rendered
        assert "assess_snapshot" in rendered
        assert "416.5ms" in rendered
        assert rendered.count("일치") >= 3  # 생성·배치·측정 재현 3계열

    def test_llm_section_shows_extraction_accuracy_and_judge_config(self, rendered: str) -> None:
        assert "LLM 평가" in rendered
        assert "85.0%" in rendered  # 추출 정확도 매크로
        assert "실험 결과 파일 확인됨" in rendered  # judge 요약(존재)
        assert "gpt-5" in rendered
        assert "claude-opus-5" in rendered
        assert "rubric v1" in rendered


# ---------------------------------------------------------------------------
# 전부 없음 — "실측 전" 렌더
# ---------------------------------------------------------------------------


class TestAllReportsAbsent:
    @pytest.fixture()
    def rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        report_paths = _empty_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)  # eval/config.yaml도 미생성

        at = AppTest.from_function(_run_evaluation_page)
        at.run()
        assert not at.exception
        return _rendered_text(at)

    def test_renders_without_exception_and_all_sections_present(self, rendered: str) -> None:
        for title in ("감지 성능", "수요예측", "플랫폼 검증", "LLM 평가"):
            assert title in rendered

    def test_pending_badge_appears_for_every_missing_block(self, rendered: str) -> None:
        # 감지 성능 본문 1(블라인드 1·2차는 둘 다 없으면 서브블록 자체를 생략 — 별도
        # 테스트 test_blind_mechanism_phrase_omitted_when_no_blind_data로 고정) +
        # 수요예측 1 + E2E 1 + perf 1 + 재현성 3계열 + 추출 정확도 1 + judge 요약 1 = 9회.
        assert rendered.count("실측 전") >= 9

    def test_no_fabricated_numbers_leak_through(self, rendered: str) -> None:
        # 합성 픽스처 값(90.0%, cand-F 등)이 전부 없는 경로에서 우연히 나타나지 않는지.
        assert "cand-F" not in rendered
        assert "90.0%" not in rendered

    def test_blind_mechanism_phrase_omitted_when_no_blind_data(self, rendered: str) -> None:
        # 둘 다 없으면 메커니즘 문구 자체가 무의미하므로 렌더하지 않는다.
        assert "탐지기 동일" not in rendered


# ---------------------------------------------------------------------------
# 1개 리포트 손상 — 그 블록만 격리, 나머지(별도 파일인 블라인드 포함)는 정상 렌더
# ---------------------------------------------------------------------------


class TestOneCorruptedReport:
    @pytest.fixture()
    def rendered(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
        report_paths = _build_full_report_tree(tmp_path)
        # detection_metrics.json만 깨진 JSON으로 덮어쓴다(나머지 전부 정상 유지).
        report_paths["detection_metrics"].write_text("{이것은 유효한 JSON이 아니다", encoding="utf-8")
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        at = AppTest.from_function(_run_evaluation_page)
        at.run()
        assert not at.exception
        return _rendered_text(at)

    def test_corrupted_section_shows_pending_badge(self, rendered: str) -> None:
        assert "testhash1" not in rendered  # 깨진 파일의 값은 나오지 않는다
        assert "공고 추출 미반영" not in rendered  # 캡션도 렌더되지 않는다(본문 블록 격리)

    def test_other_file_backed_blind_summary_still_renders(self, rendered: str) -> None:
        # 블라인드 요약은 detection_metrics.json과 별개 파일이라 영향을 받지 않는다.
        assert "35.0%" in rendered
        assert "100.0%" in rendered
        assert "44.67%" in rendered
        assert "탐지기 동일" in rendered

    def test_unrelated_sections_still_render_real_values(self, rendered: str) -> None:
        assert "0.3205" in rendered  # 수요예측
        assert "10/10" in rendered  # E2E
        assert "85.0%" in rendered  # 추출 정확도


# ---------------------------------------------------------------------------
# 서비스 계층 단위 검증 — AppTest 없이 load_eval_reports 직접 호출
# ---------------------------------------------------------------------------


class TestLoadEvalReports:
    def test_all_present_returns_data_for_every_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_paths = _build_full_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        reports = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())

        for key in report_paths:
            assert reports[key]["exists"] is True, key
            assert "data" in reports[key], key
        assert reports["eval_config"]["exists"] is True
        assert reports["eval_config"]["data"]["rubric_version"] == "v1"
        assert reports["eval_latest_result"]["exists"] is True

    def test_missing_file_reports_exists_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_paths = _empty_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        reports = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())

        for key in report_paths:
            assert reports[key] == {"exists": False}, key
        assert reports["eval_config"] == {"exists": False}
        assert reports["eval_latest_result"] == {"exists": False}

    def test_broken_json_is_isolated_as_error_without_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_paths = _build_full_report_tree(tmp_path)
        report_paths["forecast_mape"].write_text("not json at all {{{", encoding="utf-8")
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        reports = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())

        assert reports["forecast_mape"]["exists"] is True
        assert "error" in reports["forecast_mape"]
        assert "data" not in reports["forecast_mape"]
        # 다른 리포트는 영향받지 않는다.
        assert reports["detection_metrics"]["exists"] is True
        assert "data" in reports["detection_metrics"]

    def test_current_report_mtimes_changes_when_a_file_is_touched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report_paths = _build_full_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        before = evaluation_service.current_report_mtimes()

        # mtime 해상도보다 확실히 크게 시각을 미래로 밀어 파일을 다시 쓴다.
        target = report_paths["detection_metrics"]
        new_payload = json.dumps(_DETECTION_METRICS, ensure_ascii=False)
        target.write_text(new_payload, encoding="utf-8")
        stat = target.stat()
        os.utime(target, (stat.st_atime + 5, stat.st_mtime + 5))

        after = evaluation_service.current_report_mtimes()
        assert before != after

    def test_load_eval_reports_cache_reflects_new_mtime_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """캐시 clear 관례: mtimes 인자가 바뀌면(호출부가 새 튜플을 넘기면) 캐시가 갱신된
        결과를 돌려준다 — 옛 mtimes로 다시 부르면 여전히 옛 캐시가 남아있을 수 있으므로
        반드시 새로 계산한 current_report_mtimes()를 넘겨야 한다는 계약을 고정한다."""
        report_paths = _build_full_report_tree(tmp_path)
        _activate(monkeypatch, tmp_path, report_paths=report_paths)

        first = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())
        assert first["forecast_mape"]["data"]["overall"]["ses_mape_mean"] == pytest.approx(0.3205)

        updated_payload = dict(_FORECAST_MAPE)
        updated_payload["overall"] = dict(_FORECAST_MAPE["overall"])
        updated_payload["overall"]["ses_mape_mean"] = 0.9999
        report_paths["forecast_mape"].write_text(
            json.dumps(updated_payload, ensure_ascii=False), encoding="utf-8"
        )
        stat = report_paths["forecast_mape"].stat()
        os.utime(report_paths["forecast_mape"], (stat.st_atime + 5, stat.st_mtime + 5))

        second = evaluation_service.load_eval_reports(evaluation_service.current_report_mtimes())
        assert second["forecast_mape"]["data"]["overall"]["ses_mape_mean"] == pytest.approx(0.9999)
