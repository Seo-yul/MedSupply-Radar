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
