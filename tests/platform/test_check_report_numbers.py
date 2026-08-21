"""`scripts/check_report_numbers.py` 대조 엔진 테스트(Task S-32).

검증 리포트의 신뢰는 "문서의 수치가 reports/의 JSON과 기계적으로 같다"에 걸려 있고, 그
대조를 하는 것이 이 스크립트다. 그러므로 여기서 고정해야 하는 것은 세 가지다.

1. **일치를 일치라고 한다** — 반올림 자리·퍼센트 환산이 표기 규칙대로 동작한다.
2. **불일치를 놓치지 않는다** — 값이 어긋나면 반드시 FAIL로 잡는다(가장 중요).
3. **마킹 누락을 놓치지 않는다** — 문서에 슬쩍 들어온 미대조 수치를 목록으로 낸다.

픽스처는 전부 tmp_path에 만든 소형 md + JSON이다(저장소 실파일에 의존하지 않는다). 마지막
클래스만 실제 `docs/verification-report.md`를 대조해 산출물 자체를 회귀 고정한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_report_numbers as crn  # noqa: E402


# ---------------------------------------------------------------------------
# 픽스처 헬퍼
# ---------------------------------------------------------------------------


def write_json(base: Path, rel: str, payload: dict) -> None:
    path = base / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_md(base: Path, body: str) -> Path:
    path = base / "report.md"
    path.write_text(body, encoding="utf-8")
    return path


SAMPLE = {
    "results": {
        "detection_rate": 0.9,
        "false_positive_rate": 0.4519230769230769,
        "counts": {"detected": 18, "labeled": 20},
        "lead_days": {"median": 26.5},
        "excluded": [{"item_id": "ITM-0017"}, {"item_id": "ITM-0026"}],
    },
    "overall": {"baseline_improved": -0.0256},
    "verdict": True,
    "anchor": "c34bf4cb",
}


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    write_json(tmp_path, "reports/sample.json", SAMPLE)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. JSON 경로 해석(점 표기 + 배열 인덱스만 — 의도적으로 단순한 문법)
# ---------------------------------------------------------------------------


class TestResolvePath:
    def test_dot_path(self) -> None:
        assert crn.resolve_path(SAMPLE, "results.counts.detected") == 18

    def test_array_index(self) -> None:
        assert crn.resolve_path(SAMPLE, "results.excluded[1].item_id") == "ITM-0026"

    def test_top_level_scalar(self) -> None:
        assert crn.resolve_path(SAMPLE, "verdict") is True

    def test_missing_key_raises_with_path_in_message(self) -> None:
        with pytest.raises(crn.PathError) as exc:
            crn.resolve_path(SAMPLE, "results.nope.deeper")
        assert "results.nope" in str(exc.value)

    def test_index_out_of_range_raises(self) -> None:
        with pytest.raises(crn.PathError):
            crn.resolve_path(SAMPLE, "results.excluded[9].item_id")

    def test_index_on_non_list_raises(self) -> None:
        with pytest.raises(crn.PathError):
            crn.resolve_path(SAMPLE, "results.counts[0]")


# ---------------------------------------------------------------------------
# 2. 표기값 ↔ JSON 실값 비교(반올림 자리는 표기가 선언한다)
# ---------------------------------------------------------------------------


class TestCompareLiteral:
    @pytest.mark.parametrize(
        ("value", "literal"),
        [
            (0.9, "90.0%"),
            (0.9, "90%"),
            (0.4519230769230769, "45.2%"),
            (0.4519230769230769, "45.19%"),
            (-0.0256, "-2.56%p"),
            (26.5, "26.5"),
            (18, "18"),
            (416.5, "416.5"),
            (1.0, "100.0%"),
            (0.0, "0.0%"),
        ],
    )
    def test_matching_values(self, value: float, literal: str) -> None:
        ok, formatted = crn.compare_literal(value, literal)
        assert ok, f"{value} vs {literal} → {formatted}"

    @pytest.mark.parametrize(
        ("value", "literal"),
        [
            (0.9, "90.1%"),
            (0.4519230769230769, "45.1%"),
            (18, "19"),
            (26.5, "26.6"),
            (-0.0256, "2.56%p"),
        ],
    )
    def test_mismatching_values(self, value: float, literal: str) -> None:
        ok, _ = crn.compare_literal(value, literal)
        assert not ok

    def test_rounding_is_half_up(self) -> None:
        """0.4467 → 44.67%는 자리수 그대로, 44.7%는 half-up 반올림 결과여야 한다."""
        assert crn.compare_literal(0.4467, "44.67%")[0]
        assert crn.compare_literal(0.4467, "44.7%")[0]
        assert not crn.compare_literal(0.4467, "44.6%")[0]

    def test_boolean_literal(self) -> None:
        assert crn.compare_literal(True, "true")[0]
        assert not crn.compare_literal(False, "true")[0]
        assert not crn.compare_literal(1, "true")[0]

    def test_string_literal_requires_quotes(self) -> None:
        assert crn.compare_literal("c34bf4cb", '"c34bf4cb"')[0]
        assert not crn.compare_literal("c34bf4cb", '"deadbeef"')[0]

    def test_non_numeric_value_against_numeric_literal_is_mismatch(self) -> None:
        ok, formatted = crn.compare_literal(None, "18")
        assert not ok
        assert "null" in formatted or "None" in formatted


# ---------------------------------------------------------------------------
# 3. 마킹 파싱(줄 끝 주석 / 앞줄에 붙는 전용 주석줄)
# ---------------------------------------------------------------------------


class TestParseMarks:
    def test_mark_on_content_line(self) -> None:
        doc = "감지율 90.0%다. <!-- check: reports/sample.json:results.detection_rate = 90.0% -->"
        marks, skips = crn.parse_marks(doc.splitlines())
        assert skips == []
        assert len(marks) == 1
        assert marks[0].source == "reports/sample.json"
        assert marks[0].path == "results.detection_rate"
        assert marks[0].literal == "90.0%"
        assert marks[0].target_line_no == 1

    def test_mark_line_attaches_to_previous_content_line(self) -> None:
        doc = (
            "| 감지율 | 90.0% |\n"
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n"
        )
        marks, _ = crn.parse_marks(doc.splitlines())
        assert len(marks) == 1
        assert marks[0].target_line_no == 1
        assert marks[0].mark_line_no == 2

    def test_multiple_marks_on_one_mark_line(self) -> None:
        doc = (
            "| 감지 | 18/20 |\n"
            "<!-- check: reports/sample.json:results.counts.detected = 18 -->"
            "<!-- check: reports/sample.json:results.counts.labeled = 20 -->\n"
        )
        marks, _ = crn.parse_marks(doc.splitlines())
        assert [m.literal for m in marks] == ["18", "20"]
        assert {m.target_line_no for m in marks} == {1}

    def test_skip_directive_is_parsed_with_reason(self) -> None:
        doc = "표본 5건이다. <!-- check-skip: 구조적 개수(측정값 아님) -->"
        marks, skips = crn.parse_marks(doc.splitlines())
        assert marks == []
        assert len(skips) == 1
        assert skips[0].reason == "구조적 개수(측정값 아님)"

    def test_mark_line_after_blank_line_is_an_error(self) -> None:
        """빈 줄 뒤의 마킹 전용 줄은 붙을 대상이 없다 — 조용히 무시하지 않는다."""
        doc = "\n<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n"
        with pytest.raises(crn.MarkSyntaxError):
            crn.parse_marks(doc.splitlines())

    def test_malformed_mark_raises(self) -> None:
        doc = "값 <!-- check: 등호도_경로도_없음 -->"
        with pytest.raises(crn.MarkSyntaxError):
            crn.parse_marks(doc.splitlines())

    def test_mark_inside_code_span_is_documentation_not_a_directive(self) -> None:
        """컨벤션을 설명하는 문서가 자기 예시 때문에 문법 오류로 죽으면 안 된다."""
        doc = "형식은 `<!-- check: {파일}:{경로} = {표기값} -->` 이다."
        marks, skips = crn.parse_marks(doc.splitlines())
        assert marks == []
        assert skips == []


# ---------------------------------------------------------------------------
# 4. 숫자 토큰화·허용 규칙(미마킹 스캔의 기반)
# ---------------------------------------------------------------------------


class TestTokenScan:
    def test_percent_and_percent_point_are_single_tokens(self) -> None:
        assert crn.number_tokens("감지 90.0%, 델타 -2.56%p, 건수 18") == ["90.0%", "-2.56%p", "18"]

    def test_unit_suffix_is_not_part_of_token(self) -> None:
        assert crn.number_tokens("p95 416.5ms, 중앙 26.5일, 20건") == ["416.5", "26.5", "20"]

    def test_iso_dates_are_not_tokens(self) -> None:
        text, allowed = crn.visible_text("스윕 2026-07-01 ~ 2026-08-01")
        assert crn.number_tokens(text) == []
        assert [row["rule"] for row in allowed] == ["iso_date", "iso_date"]

    def test_code_spans_are_excluded(self) -> None:
        text, allowed = crn.visible_text("해시는 `c34bf4cb9215` 이고 감지 90.0%다")
        assert crn.number_tokens(text) == ["90.0%"]
        assert any(row["rule"] == "code_span" for row in allowed)

    def test_identifiers_and_years_are_allowed(self) -> None:
        text, allowed = crn.visible_text("ITM-0011은 S-30b가 2026년에 §4.3에서 다뤘다(W2·v1)")
        assert crn.number_tokens(text) == []
        assert {row["rule"] for row in allowed} >= {"identifier", "year"}

    def test_heading_and_list_numbering_allowed(self) -> None:
        assert crn.number_tokens(crn.visible_text("## 2. 감지 성능")[0]) == []
        assert crn.number_tokens(crn.visible_text("1. 첫 항목")[0]) == []

    def test_table_separator_row_allowed(self) -> None:
        assert crn.number_tokens(crn.visible_text("| --- | --- |")[0]) == []

    def test_korean_ordinals_are_allowed(self) -> None:
        """'1차 블라인드'의 1은 회차 번호지 측정값이 아니다."""
        text, allowed = crn.visible_text("1차와 2차를 3단계로 나눠 1주차에 봤다")
        assert crn.number_tokens(text) == []
        assert {row["rule"] for row in allowed} == {"ko_ordinal"}


# ---------------------------------------------------------------------------
# 5. 종단 대조 — 일치 / 불일치 / 미마킹
# ---------------------------------------------------------------------------


class TestCheckReport:
    def test_all_marks_match_is_pass(self, base: Path) -> None:
        md = write_md(
            base,
            "| 감지율 | 90.0% | 18/20 |\n"
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->"
            "<!-- check: reports/sample.json:results.counts.detected = 18 -->"
            "<!-- check: reports/sample.json:results.counts.labeled = 20 -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "PASS"
        assert report["summary"]["marks"] == 3
        assert report["summary"]["mismatches"] == 0
        assert report["summary"]["unmarked"] == 0

    def test_value_mismatch_is_caught(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율은 91.0%다. "
            "<!-- check: reports/sample.json:results.detection_rate = 91.0% -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["summary"]["mismatches"] == 1
        bad = report["mismatches"][0]
        assert bad["literal"] == "91.0%"
        assert bad["expected"] == "90.0"
        assert bad["path"] == "results.detection_rate"

    def test_unmarked_number_is_reported(self, base: Path) -> None:
        md = write_md(base, "오탐률은 45.2%였고 선행은 26.5일이었다.\n")
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["summary"]["unmarked"] == 2
        assert {row["token"] for row in report["unmarked"]} == {"45.2%", "26.5"}

    def test_marked_literal_absent_from_line_is_caught(self, base: Path) -> None:
        """JSON과는 맞지만 그 표기가 정작 본문에 없는 마킹 — 유령 대조를 막는다."""
        md = write_md(
            base,
            "본문에는 다른 말만 있다. "
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["mismatches"][0]["kind"] == "literal_not_in_line"

    def test_boolean_mark_needs_no_literal_in_line(self, base: Path) -> None:
        """`verdict = true`를 우리말로 서술한 줄에도 마킹이 붙을 수 있어야 한다 —
        불리언은 본문에 'true'라는 글자로 찍히지 않기 때문이다. 값 대조는 그대로 한다."""
        md = write_md(
            base,
            "판정은 통과다. <!-- check: reports/sample.json:verdict = true -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "PASS"
        assert report["marks"][0]["literal_found_in_line"] is False

    def test_boolean_mark_still_fails_on_wrong_value(self, base: Path) -> None:
        md = write_md(
            base,
            "판정은 실패다. <!-- check: reports/sample.json:verdict = false -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["mismatches"][0]["kind"] == "value_mismatch"

    def test_missing_json_path_is_a_mismatch_not_a_crash(self, base: Path) -> None:
        md = write_md(
            base,
            "값 18. <!-- check: reports/sample.json:results.counts.nope = 18 -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["mismatches"][0]["kind"] == "path_error"

    def test_missing_source_file_is_a_mismatch_not_a_crash(self, base: Path) -> None:
        md = write_md(
            base,
            "값 18. <!-- check: reports/absent.json:results.counts.detected = 18 -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["mismatches"][0]["kind"] == "source_error"

    def test_skip_directive_clears_remaining_tokens_and_is_recorded(self, base: Path) -> None:
        md = write_md(base, "시드 5건을 썼다. <!-- check-skip: 구조적 개수 -->\n")
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "PASS"
        assert report["summary"]["unmarked"] == 0
        assert report["skips"][0]["reason"] == "구조적 개수"
        assert report["skips"][0]["tokens"] == ["5"]

    def test_skip_does_not_hide_a_wrong_mark_on_the_same_line(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 91.0%, 시드 5건. "
            "<!-- check: reports/sample.json:results.detection_rate = 91.0% -->"
            "<!-- check-skip: 구조적 개수 -->\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "FAIL"
        assert report["summary"]["mismatches"] == 1

    def test_fenced_code_block_is_skipped_entirely(self, base: Path) -> None:
        md = write_md(
            base,
            "명령:\n\n```bash\npython scripts/measure_perf.py --repeats 30\n```\n\n끝.\n",
        )
        report = crn.check_report(md, base)
        assert report["summary"]["status"] == "PASS"
        assert report["summary"]["unmarked"] == 0

    def test_source_files_are_hashed_for_audit(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 90.0%. "
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n",
        )
        report = crn.check_report(md, base)
        assert len(report["sources"]["reports/sample.json"]["sha256"]) == 64

    def test_required_paths_must_be_marked(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 90.0%. "
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n",
        )
        required = {"reports/sample.json": ("results.detection_rate", "results.lead_days.median")}
        report = crn.check_report(md, base, required=required)
        assert report["summary"]["status"] == "FAIL"
        assert report["required_paths"]["missing"] == [
            "reports/sample.json:results.lead_days.median"
        ]

    def test_required_paths_satisfied(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 90.0%, 선행 26.5일. "
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->"
            "<!-- check: reports/sample.json:results.lead_days.median = 26.5 -->\n",
        )
        required = {"reports/sample.json": ("results.detection_rate", "results.lead_days.median")}
        report = crn.check_report(md, base, required=required)
        assert report["summary"]["status"] == "PASS"
        assert report["required_paths"]["missing"] == []


# ---------------------------------------------------------------------------
# 6. CLI
# ---------------------------------------------------------------------------


class TestCli:
    def test_exit_zero_and_writes_result_json(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 90.0%. "
            "<!-- check: reports/sample.json:results.detection_rate = 90.0% -->\n",
        )
        out = base / "out" / "report_check.json"
        code = crn.main(
            ["--report", str(md), "--base-dir", str(base), "--out", str(out), "--no-required"]
        )
        assert code == 0
        assert json.loads(out.read_text(encoding="utf-8"))["summary"]["status"] == "PASS"

    def test_exit_one_on_mismatch(self, base: Path) -> None:
        md = write_md(
            base,
            "감지율 91.0%. "
            "<!-- check: reports/sample.json:results.detection_rate = 91.0% -->\n",
        )
        out = base / "report_check.json"
        code = crn.main(
            ["--report", str(md), "--base-dir", str(base), "--out", str(out), "--no-required"]
        )
        assert code == 1
        assert json.loads(out.read_text(encoding="utf-8"))["summary"]["status"] == "FAIL"


# ---------------------------------------------------------------------------
# 7. 실제 산출물 회귀 고정 — docs/verification-report.md는 항상 대조 통과여야 한다
# ---------------------------------------------------------------------------


class TestActualVerificationReport:
    def test_repository_report_passes_full_check(self) -> None:
        report = crn.check_report(
            REPO_ROOT / crn.DEFAULT_REPORT, REPO_ROOT, required=crn.REQUIRED_PATHS
        )
        assert report["mismatches"] == [], report["mismatches"]
        assert report["unmarked"] == [], report["unmarked"]
        assert report["required_paths"]["missing"] == []
        assert report["summary"]["status"] == "PASS"

    def test_required_paths_cover_the_headline_metrics(self) -> None:
        """핵심 수치(감지·오탐·선행·MAPE·E2E·p95·재현성)의 마킹 의무가 상수에서 빠지지
        않게 고정한다 — 이 목록이 비면 대조는 통과하지만 의미가 사라진다."""
        flat = {
            f"{source}:{path}"
            for source, paths in crn.REQUIRED_PATHS.items()
            for path in paths
        }
        for needed in (
            "reports/analytics/detection_metrics.json:results.detection_rate",
            "reports/analytics/detection_metrics.json:results.false_positive_rate",
            "reports/analytics/detection_metrics.json:results.lead_days.median",
            "reports/analytics/forecast_mape.json:overall.baseline_improved",
            "reports/analytics/blind_summary.json:aggregate.detection_rate.mean",
            "reports/analytics/blind_round1_rescored.json:"
            "aggregate.within_horizon_current_criterion.detected",
            "reports/analytics/blind_round2_summary.json:aggregate.detection_rate.mean",
            "reports/platform/e2e_results.json:passed_runs",
            "reports/platform/perf_results.json:targets.assess_snapshot.p95_ms",
            "reports/platform/reproducibility.json:generation.anchor_match",
        ):
            assert needed in flat, f"필수 마킹 대상 누락: {needed}"

    def test_committed_check_result_is_a_pass(self) -> None:
        """커밋된 reports/platform/report_check.json이 PASS 상태로 남아 있는지 고정한다."""
        result = json.loads(
            (REPO_ROOT / "reports/platform/report_check.json").read_text(encoding="utf-8")
        )
        assert result["summary"]["status"] == "PASS"
        assert result["summary"]["mismatches"] == 0
        assert result["summary"]["unmarked"] == 0
