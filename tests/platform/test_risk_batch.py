"""Task S-15: scripts/run_risk_batch.py 위험도 배치 실행기 테스트.

task-S15-brief.md 계약을 검증한다. 스냅샷 생성(scripts/generate_dataset.py --baseline-only)
과 배치 실행(scripts/run_risk_batch.py) 모두 subprocess로 별도 프로세스에서 실행해, 실제
CLI 계약(인자 파싱·종료 코드·stdout)까지 종단으로 검증한다.

픽스처 설계: 스냅샷 생성은 수 초 걸리므로(브리프 명시) 모듈 1회만 생성(base_snapshot,
scope='module')하고, 각 테스트는 그 파일을 함수별 tmp_path로 복사(db_path, 기본 scope)해
쓴다 — 배치 실행은 DB를 변경하므로(risk_results/forecasts insert, data_version 증가)
테스트 간 격리가 필요하다.

tests/platform은 앱측(medsupply 사용) 테스트 영역이라 scripts/datagen 테스트와 달리
medsupply를 자유롭게 import한다(tests/platform/test_views_smoke.py 등과 동일한 관례).
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from medsupply.analytics.params import load_params

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"
PARAMS_PATH = REPO_ROOT / "config" / "analytics_params.toml"

SEED = 20260801
BASE_DATE = "2026-08-01"
AS_OF_YESTERDAY = "2026-07-31"
AS_OF_TODAY = "2026-08-01"

EXPECTED_ITEM_COUNT = 124
VALID_GRADES = {"위험", "경고", "주의", "정상"}
RUN_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}#[0-9a-f]{8}$")


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼
# ---------------------------------------------------------------------------


def _generate_snapshot(db_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, str(GENERATE_SCRIPT),
            "--baseline-only", "--seed", str(SEED), "--base-date", BASE_DATE,
            "--out", str(db_path),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _run_batch(
    db_path: Path, as_of_values: list[str], params: str | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(BATCH_SCRIPT), "--db", str(db_path)]
    for value in as_of_values:
        cmd += ["--as-of", value]
    if params is not None:
        cmd += ["--params", params]
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)


def _data_version(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        conn.close()


def _distinct_run_ids(db_path: Path, table: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return sorted(r[0] for r in conn.execute(f"SELECT DISTINCT run_id FROM {table}"))
    finally:
        conn.close()


def _row_count(db_path: Path, table: str, run_id: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _dump_risk_results(db_path: Path) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT run_id, item_id, grade, score, days_to_stockout, factors_json"
            " FROM risk_results ORDER BY run_id, item_id"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 소형(?) 표준 스냅샷 — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("risk_batch_base") / "base.db"
    _generate_snapshot(db_path)
    return db_path


@pytest.fixture()
def db_path(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


@dataclass
class BatchRunResult:
    db_path: Path
    proc: subprocess.CompletedProcess[str]
    pre_data_version: int


@pytest.fixture()
def ran_batch(db_path: Path) -> BatchRunResult:
    """--as-of 2개(전일·당일)로 배치를 1회 실행한 뒤 여러 테스트가 결과를 나눠 검증한다."""
    pre_data_version = _data_version(db_path)
    proc = _run_batch(db_path, [AS_OF_YESTERDAY, AS_OF_TODAY])
    return BatchRunResult(db_path=db_path, proc=proc, pre_data_version=pre_data_version)


# ---------------------------------------------------------------------------
# 배치 실행 + 영속화 검증
# ---------------------------------------------------------------------------


class TestBatchRun:
    def test_exit_zero(self, ran_batch: BatchRunResult) -> None:
        assert ran_batch.proc.returncode == 0, ran_batch.proc.stdout + ran_batch.proc.stderr

    def test_prints_run_summary(self, ran_batch: BatchRunResult) -> None:
        assert "총 run 수: 2" in ran_batch.proc.stdout

    def test_two_runs_with_valid_run_id_format(self, ran_batch: BatchRunResult) -> None:
        run_ids = _distinct_run_ids(ran_batch.db_path, "risk_results")
        assert len(run_ids) == 2
        assert all(RUN_ID_RE.match(run_id) for run_id in run_ids), run_ids

    def test_risk_results_row_count_equals_item_count_per_run(
        self, ran_batch: BatchRunResult
    ) -> None:
        for run_id in _distinct_run_ids(ran_batch.db_path, "risk_results"):
            assert _row_count(ran_batch.db_path, "risk_results", run_id) == EXPECTED_ITEM_COUNT

    def test_risk_results_grades_within_valid_set(self, ran_batch: BatchRunResult) -> None:
        conn = sqlite3.connect(ran_batch.db_path)
        try:
            grades = {r[0] for r in conn.execute("SELECT DISTINCT grade FROM risk_results")}
        finally:
            conn.close()
        assert grades, "risk_results가 비어 있다"
        assert grades <= VALID_GRADES, grades

    def test_factors_json_parses_with_reflected_receipts_key(
        self, ran_batch: BatchRunResult
    ) -> None:
        conn = sqlite3.connect(ran_batch.db_path)
        try:
            rows = conn.execute("SELECT factors_json FROM risk_results").fetchall()
        finally:
            conn.close()
        assert rows
        for (factors_json,) in rows:
            parsed = json.loads(factors_json)
            assert "reflected_receipts" in parsed

    def test_forecasts_two_runs_complete_with_configured_horizon(
        self, ran_batch: BatchRunResult
    ) -> None:
        expected_horizon = load_params(PARAMS_PATH).forecast.horizon_days
        run_ids = _distinct_run_ids(ran_batch.db_path, "forecasts")
        assert len(run_ids) == 2

        conn = sqlite3.connect(ran_batch.db_path)
        try:
            for run_id in run_ids:
                rows = conn.execute(
                    "SELECT horizon_days FROM forecasts WHERE run_id = ?", (run_id,)
                ).fetchall()
                assert len(rows) == EXPECTED_ITEM_COUNT
                assert all(h == expected_horizon for (h,) in rows)
        finally:
            conn.close()

    def test_data_version_increases(self, ran_batch: BatchRunResult) -> None:
        post = _data_version(ran_batch.db_path)
        assert post > ran_batch.pre_data_version


# ---------------------------------------------------------------------------
# 결정성(멱등 재실행)
# ---------------------------------------------------------------------------


class TestIdempotentRerun:
    def test_rerun_same_as_of_produces_identical_rows(self, db_path: Path) -> None:
        first = _run_batch(db_path, [AS_OF_TODAY])
        assert first.returncode == 0, first.stdout + first.stderr
        dump1 = _dump_risk_results(db_path)
        assert len(dump1) == EXPECTED_ITEM_COUNT

        second = _run_batch(db_path, [AS_OF_TODAY])
        assert second.returncode == 0, second.stdout + second.stderr
        dump2 = _dump_risk_results(db_path)

        assert dump1 == dump2
        assert len(dump2) == EXPECTED_ITEM_COUNT
        assert len(_distinct_run_ids(db_path, "risk_results")) == 1


# ---------------------------------------------------------------------------
# --params 오류 경로
# ---------------------------------------------------------------------------


class TestUnknownParamsPath:
    def test_unknown_params_path_exits_nonzero(self, db_path: Path) -> None:
        missing = REPO_ROOT / "config" / "does_not_exist_analytics_params.toml"
        assert not missing.exists()
        proc = _run_batch(db_path, [AS_OF_TODAY], params=str(missing))
        assert proc.returncode != 0
