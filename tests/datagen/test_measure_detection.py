"""Task S-16: scripts/measure_detection.py 감지 성능 측정 CLI 테스트.

task-S16-brief.md 계약을 검증한다. 두 계층으로 나뉜다.

- TestScoreSweep: 채점 로직(``score_sweep``)은 DB·라벨 파일 I/O와 분리된 순수 함수다
  (스윕 결과 dict + 라벨 리스트 → 지표 dict). 브리프의 손검산 케이스·조기 감지·미감지·
  유형별 분해·정밀도 0분모 엣지케이스를 dict 픽스처만으로 빠르게 고정한다.
- TestRunSweep 이하: 실제 스냅샷(--baseline-only 소형본, 모듈 1회 생성)에 대해 CLI 계약
  (인자 검증·모드별 파일 I/O·블라인드 격리·2단계=일괄 동등성)을 검증한다. 대부분은
  ``md.main(argv)``를 인프로세스로 호출해 빠르게 검증하고, 브리프가 명시적으로 subprocess를
  요구하는 "2단계 경로 == 일괄 실행" 동등성 검사만 실제 서브프로세스 3회(단일 실행·
  predict-only·score)로 수행한다.

scripts/measure_detection.py는 scripts/datagen/의 "medsupply 미참조" 격리 원칙과 무관한
층이다(브리프: 이 스크립트는 ground truth 라벨을 읽을 수 있는 유일한 허용 경로이며,
medsupply.analytics.pipeline.assess_snapshot을 그대로 호출한다 — scripts/run_risk_batch.py와
동일한 앱측 계층).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from medsupply.analytics.params import load_params
from medsupply.data import db
from scripts import measure_detection as md

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "measure_detection.py"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
PARAMS_PATH = REPO_ROOT / "config" / "analytics_params.toml"

SEED = 20260801
BASE_DATE = "2026-08-01"
START = "2026-07-30"
END = "2026-08-01"  # 3일 스윕(빠른 테스트용 — 실제 1차 측정은 별도로 7/1~8/1 전체 수행)

EXPECTED_ITEM_COUNT = 124


# ---------------------------------------------------------------------------
# TestScoreSweep — 순수 채점 함수(DB·파일 I/O 없음)
# ---------------------------------------------------------------------------


class TestScoreSweep:
    """브리프 손검산: 라벨 2품목(1 감지 성공/1 미감지)+정상 3품목(1 오탐)
    → 감지율 0.5, 오탐률 1/3, 선행일수(성공 1건), 정밀도(위험 판정 1개가 라벨이면 1.0).
    """

    def test_hand_calculated_case(self) -> None:
        predictions = {
            # 라벨 A: 07-05 최초 주의, 07-10 위험. stockout 07-15 → 성공(선행 10일).
            "A": {"2026-07-01": "정상", "2026-07-05": "주의", "2026-07-10": "위험"},
            # 라벨 B: 스윕 내내 정상 → first_alert 없음 → 미감지.
            "B": {"2026-07-01": "정상", "2026-07-31": "정상"},
            # 정상 품목 C: 1회 주의 판정 → 오탐.
            "C": {"2026-07-01": "정상", "2026-07-20": "주의"},
            # 정상 품목 D, E: 항상 정상.
            "D": {"2026-07-01": "정상"},
            "E": {"2026-07-01": "정상"},
        }
        labels = [
            {
                "item_id": "A",
                "scenario_type": "supply_halt",
                "onset_date": "2026-07-01",
                "stockout_date": "2026-07-15",
                "params_ref": "SC-A",
                "stockout_basis": "observed",
            },
            {
                "item_id": "B",
                "scenario_type": "demand_surge",
                "onset_date": "2026-07-01",
                "stockout_date": "2026-07-20",
                "params_ref": "SC-B",
                "stockout_basis": "extrapolated",
            },
        ]

        result = md.score_sweep(predictions, labels)

        assert result["detection_rate"] == pytest.approx(0.5)
        assert result["false_positive_rate"] == pytest.approx(1 / 3)
        assert result["danger_precision"] == pytest.approx(1.0)
        assert result["counts"] == {
            "labeled": 2,
            "normal": 3,
            "detected": 1,
            "false_positives": 1,
        }
        assert result["lead_days"] == {"min": 10, "median": 10, "mean": 10, "max": 10}

    def test_early_detection_success_when_stockout_beyond_sweep_window(self) -> None:
        """스윕이 짧아 stockout_date가 스윕 밖(미래)이어도 first_alert가 있으면 성공."""
        predictions = {
            "F": {"2026-07-01": "정상", "2026-07-03": "주의"},
        }
        labels = [
            {
                "item_id": "F",
                "scenario_type": "delivery_delay",
                "onset_date": "2026-07-01",
                "stockout_date": "2026-09-01",  # 스윕 범위(~07-03) 훨씬 밖
                "params_ref": "SC-F",
                "stockout_basis": "extrapolated",
            }
        ]

        result = md.score_sweep(predictions, labels)

        assert result["detection_rate"] == pytest.approx(1.0)
        assert result["counts"]["detected"] == 1
        expected_lead = (date(2026, 9, 1) - date(2026, 7, 3)).days
        assert result["lead_days"]["min"] == expected_lead

    def test_never_alerted_item_is_not_detected(self) -> None:
        predictions = {"G": {"2026-07-01": "정상", "2026-07-02": "정상"}}
        labels = [
            {
                "item_id": "G",
                "scenario_type": "composite",
                "onset_date": "2026-07-01",
                "stockout_date": "2026-07-10",
                "params_ref": "SC-G",
                "stockout_basis": "observed",
            }
        ]

        result = md.score_sweep(predictions, labels)

        assert result["detection_rate"] == pytest.approx(0.0)
        assert result["counts"]["detected"] == 0
        assert result["lead_days"] == {"min": None, "median": None, "mean": None, "max": None}

    def test_by_type_breakdown(self) -> None:
        predictions = {
            "A1": {"2026-07-01": "주의"},  # demand_surge, 성공
            "A2": {"2026-07-01": "정상"},  # demand_surge, 미감지
            "B1": {"2026-07-01": "위험"},  # supply_halt, 성공
        }
        labels = [
            {
                "item_id": "A1", "scenario_type": "demand_surge",
                "onset_date": "2026-06-25", "stockout_date": "2026-07-10",
                "params_ref": "SC-A1", "stockout_basis": "observed",
            },
            {
                "item_id": "A2", "scenario_type": "demand_surge",
                "onset_date": "2026-06-25", "stockout_date": "2026-07-10",
                "params_ref": "SC-A2", "stockout_basis": "observed",
            },
            {
                "item_id": "B1", "scenario_type": "supply_halt",
                "onset_date": "2026-06-25", "stockout_date": "2026-07-05",
                "params_ref": "SC-B1", "stockout_basis": "observed",
            },
        ]

        result = md.score_sweep(predictions, labels)
        by_type = result["by_type"]

        assert by_type["demand_surge"]["labeled"] == 2
        assert by_type["demand_surge"]["detected"] == 1
        assert by_type["demand_surge"]["detection_rate"] == pytest.approx(0.5)
        assert by_type["supply_halt"]["labeled"] == 1
        assert by_type["supply_halt"]["detected"] == 1
        assert by_type["supply_halt"]["detection_rate"] == pytest.approx(1.0)

    def test_danger_precision_none_when_no_item_ever_reaches_danger(self) -> None:
        predictions = {
            "H": {"2026-07-01": "주의"},
            "I": {"2026-07-01": "정상"},
        }
        labels = [
            {
                "item_id": "H", "scenario_type": "general",
                "onset_date": "2026-07-01", "stockout_date": "2026-07-20",
                "params_ref": "SC-H", "stockout_basis": "observed",
            }
        ]

        result = md.score_sweep(predictions, labels)

        assert result["danger_precision"] is None

    def test_empty_labels_yields_none_detection_rate(self) -> None:
        predictions = {"X": {"2026-07-01": "정상"}}
        result = md.score_sweep(predictions, [])

        assert result["detection_rate"] is None
        assert result["false_positive_rate"] == pytest.approx(0.0)
        assert result["counts"] == {"labeled": 0, "normal": 1, "detected": 0, "false_positives": 0}


# ---------------------------------------------------------------------------
# 픽스처 — 소형 --baseline-only 스냅샷 + 수제 라벨 파일(모듈 1회 생성)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tiny_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 소형 스냅샷. 읽기 전용으로만 쓰이므로 모듈 전체가 공유한다."""
    db_path = tmp_path_factory.mktemp("measure_detection_base") / "base.db"
    proc = subprocess.run(
        [
            sys.executable, str(GENERATE_SCRIPT),
            "--baseline-only", "--seed", str(SEED), "--base-date", BASE_DATE,
            "--out", str(db_path),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return db_path


@pytest.fixture(scope="module")
def tiny_labels(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """수제 소형 라벨 파일 — items_master.csv에 실재하는 item_id만 사용."""
    path = tmp_path_factory.mktemp("measure_detection_labels") / "labels.json"
    path.write_text(
        json.dumps(
            [
                {
                    "item_id": "ITM-0001",
                    "scenario_type": "supply_halt",
                    "onset_date": "2026-07-20",
                    "stockout_date": "2026-08-10",
                    "params_ref": "SC-TEST-1",
                    "stockout_basis": "extrapolated",
                },
                {
                    "item_id": "ITM-0002",
                    "scenario_type": "demand_surge",
                    "onset_date": "2026-07-15",
                    "stockout_date": "2026-07-31",
                    "params_ref": "SC-TEST-2",
                    "stockout_basis": "observed",
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# run_sweep — I/O 스윕 함수 직접 검증(CLI 경유 없이)
# ---------------------------------------------------------------------------


class TestRunSweep:
    def test_returns_item_by_date_grade_grid(self, tiny_snapshot: Path) -> None:
        params = load_params(PARAMS_PATH)
        conn = db.get_connection(str(tiny_snapshot))
        try:
            predictions = md.run_sweep(conn, date(2026, 7, 30), date(2026, 8, 1), params)
        finally:
            conn.close()

        assert len(predictions) == EXPECTED_ITEM_COUNT
        sample = predictions["ITM-0001"]
        assert set(sample.keys()) == {"2026-07-30", "2026-07-31", "2026-08-01"}
        assert all(grade in {"위험", "경고", "주의", "정상"} for grade in sample.values())


# ---------------------------------------------------------------------------
# CLI 모드 — 인프로세스 main(argv) 호출
# ---------------------------------------------------------------------------


class TestPredictOnlyBlindIsolation:
    def test_predict_only_never_opens_labels_file(
        self, tiny_snapshot: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "predictions.json"
        missing_labels = tmp_path / "does_not_exist.json"
        assert not missing_labels.exists()

        exit_code = md.main(
            [
                "--db", str(tiny_snapshot),
                "--labels", str(missing_labels),
                "--start", START, "--end", END,
                "--predict-only", str(out_path),
            ]
        )

        assert exit_code == 0
        assert not missing_labels.exists(), "predict-only가 존재하지 않는 --labels를 건드렸다"

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert "predictions" in payload
        assert len(payload["predictions"]) == EXPECTED_ITEM_COUNT
        assert payload["sweep"] == {"start": START, "end": END, "days": 3}
        assert payload["dataset_content_hash"]
        assert payload["config_hash"]

    def test_predict_only_succeeds_without_labels_flag_at_all(
        self, tiny_snapshot: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "predictions.json"

        exit_code = md.main(
            [
                "--db", str(tiny_snapshot),
                "--start", START, "--end", END,
                "--predict-only", str(out_path),
            ]
        )

        assert exit_code == 0
        assert out_path.exists()


class TestResultMeta:
    def test_common_meta_header_fields_complete(
        self, tiny_snapshot: Path, tiny_labels: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "out.json"

        exit_code = md.main(
            [
                "--db", str(tiny_snapshot),
                "--labels", str(tiny_labels),
                "--start", START, "--end", END,
                "--out", str(out_path),
            ]
        )

        assert exit_code == 0
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        assert set(payload.keys()) == {"meta", "results"}
        meta = payload["meta"]
        for key in (
            "dataset_content_hash", "config_hash", "labels_version",
            "params_ref", "generated_at", "measured_by",
        ):
            assert key in meta and meta[key], key
        assert meta["labels_version"] == "labels.json"
        assert meta["measured_by"] == "scripts/measure_detection.py"
        assert meta["params_ref"] == str(PARAMS_PATH.name) or meta["params_ref"] == "config/analytics_params.toml"

        results = payload["results"]
        for key in (
            "detection_rate", "lead_days", "false_positive_rate", "danger_precision",
            "by_type", "sweep", "counts",
        ):
            assert key in results, key
        assert results["counts"]["labeled"] == 2
        assert results["counts"]["normal"] == EXPECTED_ITEM_COUNT - 2
        assert results["sweep"] == {"start": START, "end": END, "days": 3}


class TestExtractionStateMeta:
    """Task X-3: meta.extraction_state — 공고 반영 상태의 DB 기계 기록(브리프 §1: 테스트 1건).

    기존 출력 스키마·감지 산식은 전혀 바뀌지 않는다(키 추가만) — 이 테스트는 그 새 키의
    집계 로직만 순수하게 손검산한다(``TestScoreSweep``과 동일한 관례). 자체 최소 DB를
    구성해 네 값이 서로 뒤섞이지 않고 각각 정확히 구분되게 만든다 — 특히
    ``active_escalations``는 같은 params_hash 패밀리의 **구run**(2026-07-31, 3건 상향)이
    아니라 **최신 run**(2026-08-01, 1건 상향)만 세는지를, 두 run의 상향 건수를 서로 다르게
    둬 판별한다(버그로 전 run을 합산하면 4, 구run만 집으면 3이 나와 정답 1과 갈린다).
    """

    def test_counts_notices_mapping_and_latest_run_escalations(self) -> None:
        conn = db.get_connection(":memory:")
        db.init_db(conn, drop=False)
        try:
            conn.executemany(
                "INSERT INTO items(item_id, item_name) VALUES (?, ?)",
                [(f"ITM-{i}", f"테스트 품목 {i}") for i in range(1, 6)],
            )
            conn.executemany(
                "INSERT INTO notices(notice_id, published_date, title) VALUES (?, ?, ?)",
                [(f"N-{i}", "2026-07-01", f"테스트 공고 {i}") for i in range(1, 4)],
            )
            conn.executemany(
                "INSERT INTO notice_extractions(notice_id, payload_json, status)"
                " VALUES (?, '{}', '자동확정')",
                [(f"N-{i}",) for i in range(1, 4)],  # extracted_notices = 3
            )
            conn.executemany(
                "INSERT INTO notice_item_map(notice_id, item_id, match_basis, needs_review)"
                " VALUES (?, ?, 'ingredient', ?)",
                [
                    ("N-1", "ITM-1", 0), ("N-1", "ITM-2", 1),  # mapped_rows = 5,
                    ("N-2", "ITM-3", 0), ("N-2", "ITM-4", 1),  # needs_review_rows = 2
                    ("N-3", "ITM-5", 0),
                ],
            )
            conn.executemany(
                "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
                " escalated_by_notice) VALUES (?, ?, ?, '정상', '정상', ?)",
                [
                    # 구run(같은 패밀리 aaaa1111, as_of 앞선다) — 상향 3건. 최신 run 판정
                    # (queries.get_latest_runs)에서 제외돼야 하는 행들이다.
                    ("2026-07-31#aaaa1111", "ITM-1", "2026-07-31", 1),
                    ("2026-07-31#aaaa1111", "ITM-2", "2026-07-31", 1),
                    ("2026-07-31#aaaa1111", "ITM-3", "2026-07-31", 1),
                    # 최신 run — 상향 1건만(ITM-1). active_escalations의 정답은 이 1건뿐이다.
                    ("2026-08-01#aaaa1111", "ITM-1", "2026-08-01", 1),
                    ("2026-08-01#aaaa1111", "ITM-2", "2026-08-01", 0),
                    ("2026-08-01#aaaa1111", "ITM-3", "2026-08-01", 0),
                    ("2026-08-01#aaaa1111", "ITM-4", "2026-08-01", 0),
                    ("2026-08-01#aaaa1111", "ITM-5", "2026-08-01", 0),
                ],
            )
            conn.commit()

            state = md._extraction_state(conn)
        finally:
            conn.close()

        assert state == {
            "extracted_notices": 3,
            "mapped_rows": 5,
            "needs_review_rows": 2,
            "active_escalations": 1,
        }


class TestUnscoreableLabelDiagnostics:
    """Task S-30c B: 채점 규칙상 성공이 불가능한 라벨(stockout_date < 스윕 시작)을 세어
    진단으로 병기한다. S-30b: 1차 블라인드 라벨 20건 중 12건이 그 상태였는데 리포트에
    아무 흔적도 남지 않아 35%라는 수치가 "일반화 성능"으로 오독됐다.

    **채점 산식은 바꾸지 않는다** — raw 감지율·오탐률·counts는 진단 추가 전후 완전히
    같아야 한다(진단 병기일 뿐이다).
    """

    _PREDICTIONS = {
        "A": {"2026-07-01": "위험", "2026-07-15": "위험"},
        "B": {"2026-07-01": "정상", "2026-07-20": "주의"},
        "N": {"2026-07-01": "정상"},
    }
    _LABELS = [
        # A: 스윕(07-01~08-01) 시작 **이전** 품절 — first_alert가 아무리 일러도 07-01이라
        # first_alert <= stockout_date가 구조적으로 성립 불가(S-30b H1a).
        {"item_id": "A", "scenario_type": "supply_halt", "stockout_date": "2026-05-28"},
        {"item_id": "B", "scenario_type": "demand_surge", "stockout_date": "2026-08-10"},
    ]
    _SWEEP_START = date(2026, 7, 1)
    _SWEEP_END = date(2026, 8, 1)

    def test_counts_and_lists_unscoreable_labels(self) -> None:
        result = md.score_sweep(
            self._PREDICTIONS, self._LABELS, sweep_start=self._SWEEP_START,
            sweep_end=self._SWEEP_END, horizon_days=30,
        )
        diag = result["unscoreable_labels"]
        assert diag["counts"] == {"labeled_total": 2, "unscoreable": 1}
        assert diag["sweep_start"] == "2026-07-01"
        assert diag["labels"] == [
            {"item_id": "A", "scenario_type": "supply_halt", "stockout_date": "2026-05-28"}
        ]
        assert "first_alert" in diag["criterion"]

    def test_raw_metrics_are_identical_with_and_without_the_diagnostic(self) -> None:
        without = md.score_sweep(
            self._PREDICTIONS, self._LABELS, sweep_end=self._SWEEP_END, horizon_days=30
        )
        with_diag = md.score_sweep(
            self._PREDICTIONS, self._LABELS, sweep_start=self._SWEEP_START,
            sweep_end=self._SWEEP_END, horizon_days=30,
        )
        for key in ("detection_rate", "false_positive_rate", "danger_precision",
                    "lead_days", "by_type", "counts", "threshold_warning", "threshold_danger"):
            assert with_diag[key] == without[key], key
        assert without["unscoreable_labels"] is None

    def test_no_unscoreable_labels_reports_zero(self) -> None:
        result = md.score_sweep(
            self._PREDICTIONS, self._LABELS[1:], sweep_start=self._SWEEP_START,
            sweep_end=self._SWEEP_END, horizon_days=30,
        )
        assert result["unscoreable_labels"]["counts"] == {
            "labeled_total": 1, "unscoreable": 0
        }
        assert result["unscoreable_labels"]["labels"] == []

    def test_cli_writes_diagnostic_and_warns_on_stderr(
        self, tiny_snapshot: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        labels_path = tmp_path / "pre_sweep_labels.json"
        labels_path.write_text(
            json.dumps(
                [
                    {
                        "item_id": "ITM-0001", "scenario_type": "supply_halt",
                        "onset_date": "2026-04-01", "stockout_date": "2026-05-28",
                        "params_ref": "SC-X", "stockout_basis": "observed",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out_path = tmp_path / "diag.json"
        assert md.main(
            [
                "--db", str(tiny_snapshot), "--labels", str(labels_path),
                "--start", START, "--end", END, "--out", str(out_path),
            ]
        ) == 0

        captured = capsys.readouterr()
        assert "채점 불가" in captured.err

        results = json.loads(out_path.read_text(encoding="utf-8"))["results"]
        assert results["unscoreable_labels"]["counts"]["unscoreable"] == 1


class TestWithinHorizonLowerBound:
    """Task S-30c B: within_horizon에 하한(stockout >= 스윕 시작)을 추가한다.

    S-30b (c)①: 기존 기준은 상한뿐이라 "스윕이 맞힐 기회가 있었던 라벨만"이라는 선언과
    달리 스윕 시작 이전 품절 라벨을 지평 안에 그대로 남겼다 — threshold_metrics의 성공
    규칙(first_alert <= stockout_date)에서 항상 거짓인 라벨들이다.
    """

    _LABELS = [
        {"item_id": "EARLY", "scenario_type": "supply_halt", "stockout_date": "2026-05-28"},
        {"item_id": "IN", "scenario_type": "demand_surge", "stockout_date": "2026-08-10"},
        {"item_id": "LATE", "scenario_type": "delivery_delay", "stockout_date": "2026-09-17"},
    ]

    def test_lower_bound_excludes_pre_sweep_labels(self) -> None:
        in_horizon, excluded = md.split_labels_by_horizon(
            self._LABELS, date(2026, 8, 1), 30, sweep_start=date(2026, 7, 1)
        )
        assert [row["item_id"] for row in in_horizon] == ["IN"]
        assert sorted(row["item_id"] for row in excluded) == ["EARLY", "LATE"]

    def test_without_sweep_start_keeps_legacy_upper_bound_only(self) -> None:
        in_horizon, excluded = md.split_labels_by_horizon(self._LABELS, date(2026, 8, 1), 30)
        assert sorted(row["item_id"] for row in in_horizon) == ["EARLY", "IN"]
        assert [row["item_id"] for row in excluded] == ["LATE"]

    def test_criterion_string_states_both_bounds(self) -> None:
        result = md.score_sweep(
            {"IN": {"2026-07-01": "정상"}, "EARLY": {"2026-07-01": "위험"},
             "LATE": {"2026-07-01": "정상"}},
            self._LABELS, sweep_start=date(2026, 7, 1), sweep_end=date(2026, 8, 1),
            horizon_days=30,
        )
        criterion = result["within_horizon"]["criterion"]
        assert "sweep_start <= stockout_date" in criterion
        assert "sweep_end + grade.watch_days" in criterion
        assert result["within_horizon"]["sweep_start"] == "2026-07-01"
        assert result["within_horizon"]["counts"] == {
            "labeled_total": 3, "labeled_in_horizon": 1, "excluded": 2
        }


class TestPreservesForeignSections:
    """재측정이 결과 파일의 {meta, results} 밖 최상위 키를 지우지 않는다(S-17 리뷰 F5).

    calibration(캘리브레이션 후보 비교표·채택 사유·동결 선언)처럼 사람이 덧붙인 기록이
    측정 한 번에 사라지면 안 된다.
    """

    def _measure(self, snapshot: Path, labels: Path, out_path: Path) -> int:
        return md.main(
            [
                "--db", str(snapshot),
                "--labels", str(labels),
                "--start", START, "--end", END,
                "--out", str(out_path),
            ]
        )

    def test_extra_top_level_keys_survive_rerun(
        self, tiny_snapshot: Path, tiny_labels: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "out.json"

        assert self._measure(tiny_snapshot, tiny_labels, out_path) == 0
        first = json.loads(out_path.read_text(encoding="utf-8"))

        # 사람이 덧붙인 기록을 모사한다.
        first["calibration"] = {"adopted": "cand-F", "freeze": {"params_hash": "6ec9bf05"}}
        first["hand_note"] = "보존되어야 한다"
        out_path.write_text(json.dumps(first, ensure_ascii=False, indent=2), encoding="utf-8")

        assert self._measure(tiny_snapshot, tiny_labels, out_path) == 0
        second = json.loads(out_path.read_text(encoding="utf-8"))

        assert second["calibration"] == {
            "adopted": "cand-F", "freeze": {"params_hash": "6ec9bf05"}
        }
        assert second["hand_note"] == "보존되어야 한다"
        # meta·results는 새 측정값으로 갱신된다(보존 대상이 아니다).
        assert set(second) == {"meta", "results", "calibration", "hand_note"}
        assert second["results"]["counts"] == first["results"]["counts"]

    def test_fresh_file_has_only_meta_and_results(
        self, tiny_snapshot: Path, tiny_labels: Path, tmp_path: Path
    ) -> None:
        """기존 파일이 없으면 보존할 것도 없다 — 스키마는 종전 그대로."""
        out_path = tmp_path / "fresh.json"

        assert self._measure(tiny_snapshot, tiny_labels, out_path) == 0

        assert set(json.loads(out_path.read_text(encoding="utf-8"))) == {"meta", "results"}

    def test_corrupt_existing_file_does_not_fail_measurement(
        self, tiny_snapshot: Path, tiny_labels: Path, tmp_path: Path
    ) -> None:
        """기존 파일이 깨져 있어도 측정은 성공한다(보존만 건너뛴다)."""
        out_path = tmp_path / "corrupt.json"
        out_path.write_text("{ not valid json", encoding="utf-8")

        assert self._measure(tiny_snapshot, tiny_labels, out_path) == 0

        assert set(json.loads(out_path.read_text(encoding="utf-8"))) == {"meta", "results"}


class TestArgValidation:
    def test_predict_only_and_score_together_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            md.main(
                [
                    "--predict-only", str(tmp_path / "a.json"),
                    "--score", str(tmp_path / "b.json"),
                ]
            )
        assert exc_info.value.code == 2

    def test_predict_only_requires_db_start_end(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            md.main(["--predict-only", str(tmp_path / "a.json")])
        assert exc_info.value.code == 2

    def test_score_requires_labels_and_out(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            md.main(["--score", str(tmp_path / "preds.json")])
        assert exc_info.value.code == 2

    def test_default_mode_requires_labels_and_out(self, tiny_snapshot: Path) -> None:
        with pytest.raises(SystemExit) as exc_info:
            md.main(["--db", str(tiny_snapshot), "--start", START, "--end", END])
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# 2단계 경로(predict-only → score) == 일괄 실행 동등성 — subprocess 3회
# ---------------------------------------------------------------------------


def _run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, cwd=REPO_ROOT,
    )


class TestTwoPhaseEquivalence:
    def test_predict_only_then_score_matches_single_shot(
        self, tiny_snapshot: Path, tiny_labels: Path, tmp_path: Path
    ) -> None:
        out_full = tmp_path / "out_full.json"
        proc_full = _run_cli(
            [
                "--db", str(tiny_snapshot), "--labels", str(tiny_labels),
                "--start", START, "--end", END, "--out", str(out_full),
            ]
        )
        assert proc_full.returncode == 0, proc_full.stdout + proc_full.stderr

        preds_path = tmp_path / "preds.json"
        proc_predict = _run_cli(
            [
                "--db", str(tiny_snapshot),
                "--start", START, "--end", END,
                "--predict-only", str(preds_path),
            ]
        )
        assert proc_predict.returncode == 0, proc_predict.stdout + proc_predict.stderr

        out_two_phase = tmp_path / "out_two_phase.json"
        proc_score = _run_cli(
            [
                "--score", str(preds_path),
                "--labels", str(tiny_labels),
                "--out", str(out_two_phase),
            ]
        )
        assert proc_score.returncode == 0, proc_score.stdout + proc_score.stderr

        full = json.loads(out_full.read_text(encoding="utf-8"))
        two_phase = json.loads(out_two_phase.read_text(encoding="utf-8"))

        assert full["results"] == two_phase["results"]

        meta_full = dict(full["meta"])
        meta_two_phase = dict(two_phase["meta"])
        meta_full.pop("generated_at")
        meta_two_phase.pop("generated_at")
        assert meta_full == meta_two_phase
