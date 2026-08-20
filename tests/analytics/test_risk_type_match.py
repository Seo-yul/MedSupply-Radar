"""Task S-17: risk_type 일치율 매핑 규칙 단위 테스트.

``scripts/measure_detection.py``가 S-17에서 신설한 ``risk_type_match`` 지표의 **규칙 부분**만
고정한다 — 라벨 파일(``data/scenarios/ground_truth/``)도 스냅샷 DB도 열지 않고, 전부 합성
입력(dict 픽스처)으로 검증한다. 실제 라벨과의 대조는 measure_detection.py CLI의 소관이고,
이 테스트는 그 CLI가 쓰는 순수 함수(``modal_risk_type``·``risk_type_matches``)와
``score_sweep``의 risk_type_match 조립만 본다.

브리프 §3이 정한 매핑:
    usage_surge(=demand_surge) → demand_surge
    supply_halt               → supply_halt · composite 허용
    delivery_delay            → delivery_delay · composite 허용
    compound(=composite)      → composite

핵심 계약 하나를 별도로 고정한다: risk_type_match는 **부가 지표**이므로, risk_types를 주든
말든 등급 기반 지표(감지율·오탐률·선행일수·정밀도)는 한 글자도 달라지지 않아야 한다.
"""

from __future__ import annotations

import pytest

from scripts.measure_detection import (
    ALERT_GRADES,
    DANGER_PLUS_GRADES,
    RISK_TYPE_MATCH_RULES,
    WARNING_PLUS_GRADES,
    modal_risk_type,
    risk_type_matches,
    score_sweep,
    threshold_metrics,
)


# ---------------------------------------------------------------------------
# 매핑 규칙 — risk_type_matches
# ---------------------------------------------------------------------------


class TestRiskTypeMatches:
    @pytest.mark.parametrize(
        ("scenario_type", "risk_type"),
        [
            ("demand_surge", "demand_surge"),
            ("usage_surge", "demand_surge"),  # 브리프 표기 별칭
            ("supply_halt", "supply_halt"),
            ("supply_halt", "composite"),  # composite 허용
            ("delivery_delay", "delivery_delay"),
            ("delivery_delay", "composite"),  # composite 허용
            ("composite", "composite"),
            ("compound", "composite"),  # 브리프 표기 별칭
        ],
    )
    def test_accepted_combinations(self, scenario_type: str, risk_type: str) -> None:
        assert risk_type_matches(scenario_type, risk_type) is True

    @pytest.mark.parametrize(
        ("scenario_type", "risk_type"),
        [
            # demand_surge는 composite를 허용하지 않는다(브리프가 그 둘만 허용으로 지정).
            ("demand_surge", "composite"),
            ("usage_surge", "composite"),
            # composite 라벨은 정확히 composite여야 한다 — 구성 요소 하나만 잡히면 불일치.
            ("composite", "supply_halt"),
            ("composite", "delivery_delay"),
            ("composite", "demand_surge"),
            # 요인 0개('general')는 어떤 유형과도 일치하지 않는다.
            ("demand_surge", "general"),
            ("supply_halt", "general"),
            ("delivery_delay", "general"),
            ("composite", "general"),
            # 서로 다른 단일 유형끼리는 불일치.
            ("supply_halt", "delivery_delay"),
            ("delivery_delay", "supply_halt"),
            ("demand_surge", "delivery_delay"),
        ],
    )
    def test_rejected_combinations(self, scenario_type: str, risk_type: str) -> None:
        assert risk_type_matches(scenario_type, risk_type) is False

    def test_none_risk_type_never_matches(self) -> None:
        """관측된 risk_type이 없으면(스윕에 해당 품목이 아예 없으면) 일치로 세지 않는다."""
        for scenario_type in RISK_TYPE_MATCH_RULES:
            assert risk_type_matches(scenario_type, None) is False

    def test_unknown_scenario_type_falls_back_to_exact_name_match(self) -> None:
        """규칙에 없는 scenario_type은 '같은 이름이면 일치'로 대우한다."""
        assert risk_type_matches("brand_new_type", "brand_new_type") is True
        assert risk_type_matches("brand_new_type", "composite") is False

    def test_composite_allowance_is_asymmetric_by_design(self) -> None:
        """supply_halt·delivery_delay만 composite를 허용한다(브리프 §3 매핑 그대로)."""
        assert "composite" in RISK_TYPE_MATCH_RULES["supply_halt"]
        assert "composite" in RISK_TYPE_MATCH_RULES["delivery_delay"]
        assert "composite" not in RISK_TYPE_MATCH_RULES["demand_surge"]


# ---------------------------------------------------------------------------
# 최빈값 — modal_risk_type
# ---------------------------------------------------------------------------


class TestModalRiskType:
    def test_returns_most_frequent_value(self) -> None:
        day_risk_types = {
            "2026-07-01": "general",
            "2026-07-02": "delivery_delay",
            "2026-07-03": "delivery_delay",
            "2026-07-04": "delivery_delay",
            "2026-07-05": "general",
        }
        assert modal_risk_type(day_risk_types) == "delivery_delay"

    def test_empty_observation_returns_none(self) -> None:
        assert modal_risk_type({}) is None

    def test_single_observation_returns_that_value(self) -> None:
        assert modal_risk_type({"2026-07-01": "composite"}) == "composite"

    def test_tie_is_broken_alphabetically_for_determinism(self) -> None:
        """동률(각 2일)이면 사전순 오름차순으로 끊는다 — dict 순서에 결과가 흔들리지 않게."""
        day_risk_types = {
            "2026-07-01": "supply_halt",
            "2026-07-02": "supply_halt",
            "2026-07-03": "composite",
            "2026-07-04": "composite",
        }
        assert modal_risk_type(day_risk_types) == "composite"

    def test_tie_result_is_independent_of_insertion_order(self) -> None:
        forward = {
            "2026-07-01": "delivery_delay",
            "2026-07-02": "delivery_delay",
            "2026-07-03": "composite",
            "2026-07-04": "composite",
        }
        reversed_order = dict(reversed(list(forward.items())))
        assert modal_risk_type(forward) == modal_risk_type(reversed_order)


# ---------------------------------------------------------------------------
# score_sweep 조립 — 합성 예측/risk_type/라벨만 사용(라벨 파일 미접근)
# ---------------------------------------------------------------------------


def _labels() -> list[dict]:
    return [
        {
            "item_id": "A", "scenario_type": "delivery_delay",
            "onset_date": "2026-07-01", "stockout_date": "2026-08-20",
            "params_ref": "SC-A", "stockout_basis": "extrapolated",
        },
        {
            "item_id": "B", "scenario_type": "supply_halt",
            "onset_date": "2026-07-01", "stockout_date": "2026-08-20",
            "params_ref": "SC-B", "stockout_basis": "extrapolated",
        },
        {
            "item_id": "C", "scenario_type": "demand_surge",
            "onset_date": "2026-07-01", "stockout_date": "2026-08-20",
            "params_ref": "SC-C", "stockout_basis": "extrapolated",
        },
        {
            "item_id": "D", "scenario_type": "composite",
            "onset_date": "2026-07-01", "stockout_date": "2026-08-20",
            "params_ref": "SC-D", "stockout_basis": "extrapolated",
        },
    ]


def _predictions() -> dict[str, dict[str, str]]:
    return {
        "A": {"2026-07-01": "정상", "2026-07-02": "주의"},
        "B": {"2026-07-01": "위험", "2026-07-02": "위험"},
        "C": {"2026-07-01": "정상", "2026-07-02": "정상"},  # 미감지
        "D": {"2026-07-01": "경고", "2026-07-02": "경고"},
        "N1": {"2026-07-01": "정상", "2026-07-02": "정상"},  # 정상 품목
    }


def _risk_types() -> dict[str, dict[str, str]]:
    return {
        # A: 최빈 composite → delivery_delay 라벨에 대해 허용(일치)
        "A": {"2026-07-01": "composite", "2026-07-02": "composite"},
        # B: 최빈 supply_halt → 일치
        "B": {"2026-07-01": "supply_halt", "2026-07-02": "supply_halt"},
        # C: 최빈 general → demand_surge 라벨에 불일치
        "C": {"2026-07-01": "general", "2026-07-02": "general"},
        # D: 최빈 supply_halt → composite 라벨에 불일치(구성 요소 하나만 잡힘)
        "D": {"2026-07-01": "supply_halt", "2026-07-02": "supply_halt"},
        "N1": {"2026-07-01": "general", "2026-07-02": "general"},
    }


class TestScoreSweepRiskTypeMatch:
    def test_overall_and_by_type_and_items(self) -> None:
        result = score_sweep(_predictions(), _labels(), _risk_types())
        rtm = result["risk_type_match"]

        assert rtm["counts"] == {"labeled": 4, "matched": 2}
        assert rtm["overall"] == pytest.approx(0.5)
        assert rtm["by_type"]["delivery_delay"] == {
            "labeled": 1, "matched": 1, "match_rate": pytest.approx(1.0)
        }
        assert rtm["by_type"]["supply_halt"] == {
            "labeled": 1, "matched": 1, "match_rate": pytest.approx(1.0)
        }
        assert rtm["by_type"]["demand_surge"]["matched"] == 0
        assert rtm["by_type"]["composite"]["matched"] == 0

        # items는 라벨 품목만 담고(정상 품목 N1 제외), 감사 추적용 감지 정보를 함께 싣는다.
        assert set(rtm["items"]) == {"A", "B", "C", "D"}
        assert rtm["items"]["A"] == {
            "scenario_type": "delivery_delay",
            "modal_risk_type": "composite",
            "matched": True,
            "detected": True,
            "first_alert": "2026-07-02",
        }
        assert rtm["items"]["C"]["detected"] is False
        assert rtm["items"]["C"]["first_alert"] is None
        assert rtm["items"]["D"]["matched"] is False

    def test_none_risk_types_yields_null_match_block(self) -> None:
        result = score_sweep(_predictions(), _labels(), None)
        assert result["risk_type_match"] is None

    def test_grade_metrics_are_unaffected_by_risk_types(self) -> None:
        """risk_type_match는 부가 지표다 — 등급 기반 지표는 risk_types 유무와 무관하게 동일."""
        with_types = score_sweep(_predictions(), _labels(), _risk_types())
        without_types = score_sweep(_predictions(), _labels(), None)

        for key in (
            "detection_rate", "lead_days", "false_positive_rate",
            "danger_precision", "by_type", "counts",
        ):
            assert with_types[key] == without_types[key], key

    def test_empty_labels_yields_null_overall_but_present_block(self) -> None:
        result = score_sweep(_predictions(), [], _risk_types())
        rtm = result["risk_type_match"]

        assert rtm["overall"] is None  # 0/0을 0.0으로 위장하지 않는다
        assert rtm["counts"] == {"labeled": 0, "matched": 0}
        assert rtm["items"] == {}

    def test_item_missing_from_risk_types_is_counted_as_mismatch(self) -> None:
        """스윕에 risk_type 관측이 없는 라벨 품목은 modal None → 불일치."""
        risk_types = _risk_types()
        del risk_types["B"]

        rtm = score_sweep(_predictions(), _labels(), risk_types)["risk_type_match"]

        assert rtm["items"]["B"]["modal_risk_type"] is None
        assert rtm["items"]["B"]["matched"] is False
        assert rtm["counts"]["matched"] == 1


# ---------------------------------------------------------------------------
# 이중 문턱 리포트(Task S-17b) — 합성 등급 시퀀스 손검산
# ---------------------------------------------------------------------------


#: 손검산용 등급 격자. 라벨 3품목(P·Q·R) + 정상 3품목(N1·N2·N3), stockout은 전부 2026-07-31.
#:
#: 문턱별 기대치(손계산):
#:   P: 주의 07-02 → 경고 07-05 → 위험 07-09
#:      주의+ first_alert 07-02(선행 29) / 경고+ 07-05(선행 26) / 위험+ 07-09(선행 22)
#:   Q: 주의 07-10까지만(경고·위험 없음)
#:      주의+ 07-10(선행 21) / 경고+ 없음(미감지) / 위험+ 없음(미감지)
#:   R: 스윕 내내 정상 → 세 문턱 모두 미감지
#:   N1: 주의만 1회 → 주의+ 오탐 O / 경고+ X / 위험+ X
#:   N2: 경고까지 → 주의+ O / 경고+ O / 위험+ X
#:   N3: 항상 정상 → 전부 X
_DUAL_PREDICTIONS = {
    "P": {
        "2026-07-01": "정상", "2026-07-02": "주의", "2026-07-05": "경고", "2026-07-09": "위험",
    },
    "Q": {"2026-07-01": "정상", "2026-07-10": "주의", "2026-07-20": "주의"},
    "R": {"2026-07-01": "정상", "2026-07-20": "정상"},
    "N1": {"2026-07-01": "정상", "2026-07-12": "주의"},
    "N2": {"2026-07-01": "정상", "2026-07-12": "주의", "2026-07-14": "경고"},
    "N3": {"2026-07-01": "정상"},
}


def _dual_labels() -> list[dict]:
    return [
        {
            "item_id": item_id, "scenario_type": scenario_type,
            "onset_date": "2026-07-01", "stockout_date": "2026-07-31",
            "params_ref": f"SC-{item_id}", "stockout_basis": "extrapolated",
        }
        for item_id, scenario_type in
        [("P", "supply_halt"), ("Q", "delivery_delay"), ("R", "demand_surge")]
    ]


class TestThresholdMetrics:
    """threshold_metrics를 문턱별로 직접 호출해 손검산한다(순수 함수 — 라벨 파일 미접근)."""

    def _report(self, alert_grades):
        label_by_item = {row["item_id"]: row for row in _dual_labels()}
        normal_ids = set(_DUAL_PREDICTIONS) - set(label_by_item)
        return threshold_metrics(_DUAL_PREDICTIONS, label_by_item, normal_ids, alert_grades)

    def test_watch_threshold_hand_calculation(self) -> None:
        report, per_item, fp_ids = self._report(ALERT_GRADES)

        assert report["alert_grades"] == ["위험", "경고", "주의"]
        assert report["counts"] == {
            "labeled": 3, "normal": 3, "detected": 2, "false_positives": 2
        }
        assert report["detection_rate"] == pytest.approx(2 / 3)
        assert report["false_positive_rate"] == pytest.approx(2 / 3)
        # P 선행 29일(07-02→07-31), Q 선행 21일(07-10→07-31)
        assert report["lead_days"] == {"min": 21, "median": 25.0, "mean": 25.0, "max": 29}
        assert per_item["P"]["first_alert"].isoformat() == "2026-07-02"
        assert fp_ids == ["N1", "N2"]

    def test_warning_threshold_hand_calculation(self) -> None:
        report, per_item, fp_ids = self._report(WARNING_PLUS_GRADES)

        assert report["alert_grades"] == ["위험", "경고"]
        # 감지: P만(07-05). Q는 '주의'까지라 이 문턱에서는 미감지.
        assert report["counts"] == {
            "labeled": 3, "normal": 3, "detected": 1, "false_positives": 1
        }
        assert report["detection_rate"] == pytest.approx(1 / 3)
        assert report["false_positive_rate"] == pytest.approx(1 / 3)
        assert report["lead_days"] == {"min": 26, "median": 26, "mean": 26, "max": 26}
        assert per_item["Q"]["detected"] is False
        assert per_item["Q"]["first_alert"] is None
        assert fp_ids == ["N2"]  # N1은 '주의'만이라 이 문턱에서 빠진다

    def test_danger_threshold_hand_calculation(self) -> None:
        report, _per_item, fp_ids = self._report(DANGER_PLUS_GRADES)

        assert report["alert_grades"] == ["위험"]
        assert report["counts"] == {
            "labeled": 3, "normal": 3, "detected": 1, "false_positives": 0
        }
        assert report["lead_days"] == {"min": 22, "median": 22, "mean": 22, "max": 22}
        assert fp_ids == []

    def test_by_type_breakdown_follows_threshold(self) -> None:
        watch, _, _ = self._report(ALERT_GRADES)
        warning, _, _ = self._report(WARNING_PLUS_GRADES)

        assert watch["by_type"]["delivery_delay"]["detected"] == 1
        assert watch["by_type"]["delivery_delay"]["lead_days"]["median"] == 21
        # 문턱을 올리면 delivery_delay(Q)가 감지에서 빠지고 선행일수도 n/a가 된다.
        assert warning["by_type"]["delivery_delay"]["detected"] == 0
        assert warning["by_type"]["delivery_delay"]["detection_rate"] == pytest.approx(0.0)
        assert warning["by_type"]["delivery_delay"]["lead_days"]["median"] is None
        assert warning["by_type"]["supply_halt"]["lead_days"]["median"] == 26

    def test_threshold_applies_symmetrically_to_detection_and_false_positives(self) -> None:
        """문턱을 올리면 감지 수와 오탐 수가 **둘 다** 단조 감소해야 한다(비대칭 적용 방지)."""
        watch, _, _ = self._report(ALERT_GRADES)
        warning, _, _ = self._report(WARNING_PLUS_GRADES)
        danger, _, _ = self._report(DANGER_PLUS_GRADES)

        detected = [r["counts"]["detected"] for r in (watch, warning, danger)]
        false_positives = [r["counts"]["false_positives"] for r in (watch, warning, danger)]

        assert detected == sorted(detected, reverse=True), detected
        assert false_positives == sorted(false_positives, reverse=True), false_positives
        # 모집단(분모)은 문턱과 무관하게 고정이다.
        assert {r["counts"]["labeled"] for r in (watch, warning, danger)} == {3}
        assert {r["counts"]["normal"] for r in (watch, warning, danger)} == {3}


class TestScoreSweepDualThreshold:
    """score_sweep이 기본 문턱 결과를 그대로 두고 새 섹션만 병기하는지."""

    def test_top_level_keys_equal_watch_threshold_section(self) -> None:
        result = score_sweep(_DUAL_PREDICTIONS, _dual_labels())

        # 최상위('주의 이상') 값은 threshold_metrics(ALERT_GRADES) 결과와 동일해야 한다.
        watch, _, _ = threshold_metrics(
            _DUAL_PREDICTIONS,
            {row["item_id"]: row for row in _dual_labels()},
            set(_DUAL_PREDICTIONS) - {"P", "Q", "R"},
            ALERT_GRADES,
        )
        for key in ("detection_rate", "lead_days", "false_positive_rate", "by_type", "counts"):
            assert result[key] == watch[key], key

    def test_sections_present_with_expected_shape(self) -> None:
        result = score_sweep(_DUAL_PREDICTIONS, _dual_labels())

        for key in ("threshold_warning", "threshold_danger"):
            section = result[key]
            for field in (
                "alert_grades", "detection_rate", "lead_days", "false_positive_rate",
                "by_type", "counts", "false_positive_items",
            ):
                assert field in section, f"{key}.{field}"

        assert result["threshold_warning"]["alert_grades"] == ["위험", "경고"]
        assert result["threshold_danger"]["alert_grades"] == ["위험"]
        assert result["threshold_warning"]["false_positive_items"] == ["N2"]
        assert result["threshold_danger"]["false_positive_items"] == []

    def test_dual_threshold_is_independent_of_risk_types_argument(self) -> None:
        with_types = score_sweep(_predictions(), _labels(), _risk_types())
        without_types = score_sweep(_predictions(), _labels(), None)

        assert with_types["threshold_warning"] == without_types["threshold_warning"]
        assert with_types["threshold_danger"] == without_types["threshold_danger"]

    def test_empty_labels_yields_none_detection_rate_in_sections(self) -> None:
        result = score_sweep(_DUAL_PREDICTIONS, [])

        for key in ("threshold_warning", "threshold_danger"):
            assert result[key]["detection_rate"] is None  # 0/0을 0.0으로 위장하지 않는다
            assert result[key]["counts"]["labeled"] == 0
            assert result[key]["counts"]["normal"] == len(_DUAL_PREDICTIONS)
