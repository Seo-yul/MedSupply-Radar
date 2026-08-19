"""scripts/datagen/baseline.py 결정적 베이스라인 생성기 테스트.

실제 마스터 CSV(data/reference/*.csv)와 실제 스키마(medsupply/data/schema.sql)를 사용해
tmp_path에 SQLite 스냅샷을 생성하고, 결정성·재고 항등식·값 집합·실행 시간을 검증한다.

scripts/datagen/은 medsupply 패키지를 참조하지 않는다(격리 원칙) — 이 테스트도
scripts.datagen.baseline만 import하고 medsupply는 import하지 않는다.

시나리오 config는 이 태스크(--baseline-only)에서 읽지 않으므로 여기서도 참조하지 않는다.
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from scripts.datagen.baseline import GenerationSummary, generate_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
ITEMS_CSV = REPO_ROOT / "data" / "reference" / "items_master.csv"

SEED_A = 20260801
SEED_B = 20260802
BASE_DATE = "2026-08-01"

RunResult = tuple[Path, GenerationSummary]


def _csv_row_count(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as f:
        return sum(1 for _ in csv.DictReader(f))


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


def _dump_stock_usage_daily(db_path: Path) -> list[tuple]:
    conn = _connect(db_path)
    try:
        return conn.execute(
            "SELECT item_id, date, usage_qty, incoming_qty, closing_stock"
            " FROM stock_usage_daily ORDER BY item_id, date"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def run_a(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("baseline_a") / "medsupply.db"
    summary = generate_baseline(out, seed=SEED_A, base_date=BASE_DATE)
    return out, summary


@pytest.fixture(scope="module")
def run_a_repeat(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("baseline_a_repeat") / "medsupply.db"
    summary = generate_baseline(out, seed=SEED_A, base_date=BASE_DATE)
    return out, summary


@pytest.fixture(scope="module")
def run_b(tmp_path_factory: pytest.TempPathFactory) -> RunResult:
    out = tmp_path_factory.mktemp("baseline_b") / "medsupply.db"
    summary = generate_baseline(out, seed=SEED_B, base_date=BASE_DATE)
    return out, summary


class TestDeterminism:
    """같은 (seed, base_date, CSV) → 바이트 동일, 다른 seed → 상이."""

    def test_same_seed_produces_same_content_hash(
        self, run_a: RunResult, run_a_repeat: RunResult
    ) -> None:
        _, summary_a = run_a
        _, summary_a_repeat = run_a_repeat
        assert summary_a.content_hash == summary_a_repeat.content_hash

    def test_same_seed_produces_identical_stock_usage_daily_dump(
        self, run_a: RunResult, run_a_repeat: RunResult
    ) -> None:
        path_a, _ = run_a
        path_a_repeat, _ = run_a_repeat
        assert _dump_stock_usage_daily(path_a) == _dump_stock_usage_daily(path_a_repeat)

    def test_different_seed_produces_different_content_hash(
        self, run_a: RunResult, run_b: RunResult
    ) -> None:
        _, summary_a = run_a
        _, summary_b = run_b
        assert summary_a.content_hash != summary_b.content_hash


class TestStockIdentity:
    """재고 항등식: closing[t-1] - usage[t] + incoming[t] == closing[t], closing >= 0."""

    def test_stock_identity_holds_for_first_three_items(self, run_a: RunResult) -> None:
        path, _ = run_a
        conn = _connect(path)
        try:
            item_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT item_id FROM items ORDER BY item_id LIMIT 3"
                ).fetchall()
            ]
            assert len(item_ids) == 3

            for item_id in item_ids:
                rows = conn.execute(
                    "SELECT date, usage_qty, incoming_qty, closing_stock"
                    " FROM stock_usage_daily WHERE item_id = ? ORDER BY date",
                    (item_id,),
                ).fetchall()
                assert len(rows) >= 360

                for row in rows:
                    closing = row[3]
                    assert closing >= 0

                for i in range(1, len(rows)):
                    prev_closing = rows[i - 1][3]
                    _, usage, incoming, closing = rows[i]
                    assert prev_closing - usage + incoming == closing
        finally:
            conn.close()


class TestValueRanges:
    def test_usage_qty_non_negative_and_row_count_per_item(self, run_a: RunResult) -> None:
        path, _ = run_a
        conn = _connect(path)
        try:
            (min_usage,) = conn.execute(
                "SELECT MIN(usage_qty) FROM stock_usage_daily"
            ).fetchone()
            assert min_usage is not None
            assert min_usage >= 0

            counts = conn.execute(
                "SELECT item_id, COUNT(*) FROM stock_usage_daily GROUP BY item_id"
            ).fetchall()
            assert counts
            for _, count in counts:
                assert count >= 360
        finally:
            conn.close()

    def test_incoming_shipments_status_values_and_actual_date_consistency(
        self, run_a: RunResult
    ) -> None:
        path, _ = run_a
        conn = _connect(path)
        try:
            rows = conn.execute(
                "SELECT status, actual_date, actual_qty FROM incoming_shipments"
            ).fetchall()
            assert rows, "발주가 하나도 생성되지 않았다"

            for status, actual_date, actual_qty in rows:
                assert status in {"입고 완료", "입고 예정"}
                if status == "입고 완료":
                    assert actual_date is not None
                    assert actual_qty is not None
                else:
                    assert actual_date is None
                    assert actual_qty is None
        finally:
            conn.close()

    def test_truncation_counter_is_zero(self, run_a: RunResult) -> None:
        _, summary = run_a
        assert summary.truncation_count == 0

    def test_items_row_count_matches_csv(self, run_a: RunResult) -> None:
        path, _ = run_a
        conn = _connect(path)
        try:
            (count,) = conn.execute("SELECT COUNT(*) FROM items").fetchone()
        finally:
            conn.close()
        assert count == _csv_row_count(ITEMS_CSV)


class TestPerformance:
    def test_full_generation_completes_within_60_seconds(self, run_a: RunResult) -> None:
        _, summary = run_a
        assert summary.elapsed_seconds < 60.0
