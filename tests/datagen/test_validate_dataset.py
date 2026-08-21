"""scripts/validate_dataset.py + scripts/generate_dataset.py 이력 시드 통합 테스트.

두 개의 섹션으로 나뉜다.

섹션 A(TestAllChecksPassOnValidDb 이하)는 scripts/validate_dataset.py의 11개 검사 항목을
검증한다. 실제 표준 스냅샷(124품목×365일)을 매번 생성하면 느리므로, 스키마(schema.sql)만
그대로 적용한 소형 DB(품목 3개×5일)를 직접 구성해 "정상 데이터에서는 전 항목 PASS"를
확인하고, 그 DB를 고의로 훼손해 각 항목이 FAIL을 검출하는지 검증한다.

섹션 B(TestApplyHistorySeed 이하)는 scripts/generate_dataset.py에 추가된 이력 시드 적재
통합(--skip-history-seed, content_hash 재계산, 종료 요약 시드 건수)을 검증한다. 이쪽은
scripts.datagen.baseline.generate_baseline·inject_scenarios를 실제로 호출해야 하므로
tests/datagen/test_inject.py와 동일한 실제 마스터 CSV·스키마를 쓴다.

두 섹션 모두 scripts.validate_dataset·scripts.generate_dataset만 import한다(medsupply
패키지 미참조 — 브리프의 "medsupply import 금지" 원칙을 테스트도 따른다).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

from scripts import generate_dataset as gen_cli
from scripts import validate_dataset as vd
from scripts.datagen.baseline import compute_content_hash, generate_baseline

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"
VALIDATE_SCRIPT = REPO_ROOT / "scripts" / "validate_dataset.py"
ACTION_HISTORY_SEED_CSV = REPO_ROOT / "data" / "reference" / "action_history_seed.csv"

ITEM_IDS = ["ITM-T1", "ITM-T2", "ITM-T3"]
DATES = ["2026-07-28", "2026-07-29", "2026-07-30", "2026-07-31", "2026-08-01"]
RISK_TYPES_8 = [
    "demand_surge", "demand_surge",
    "supply_halt", "supply_halt",
    "delivery_delay", "delivery_delay",
    "composite", "composite",
]

SEED_A = 20260801
BASE_DATE = "2026-08-01"


# ---------------------------------------------------------------------------
# 소형 "정상" DB 빌더 — 섹션 A 전용
# ---------------------------------------------------------------------------


def build_valid_db(path: Path) -> None:
    """스키마(schema.sql) + 정상 데이터로 채운 소형 DB. validate_dataset 11항목 전부 PASS 기대.

    품목 3개, 각 5일치 stock_usage_daily(항등식 성립·비음수), 각 2건의 incoming_shipments
    (완료 1 + 예정 1, status/actual_date/expected_qty 전부 정합), action_history 8건(유형별
    2건씩), meta 7키(content_hash는 전 데이터 적재 후 마지막에 계산해 삽입). risk_results·
    forecasts·notices·alerts는 비운다(배치/적재 전 스냅샷 전제).
    """
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

        conn.execute(
            "INSERT INTO ingredients(ingredient_code, ingredient_name_kr, ingredient_name_en,"
            " atc_code) VALUES ('ING-T1', '테스트성분', 'Test Ingredient', 'A01AA01')"
        )

        for item_id in ITEM_IDS:
            conn.execute(
                "INSERT INTO items(item_id, item_name, ingredient_code, strength, form, route,"
                " pack_size, supplier, is_essential, atc_code)"
                " VALUES (?, ?, 'ING-T1', '10mg', '정제', '경구', 10, '테스트공급사', 0, 'A01AA01')",
                (item_id, f"{item_id} 테스트정"),
            )

        for item_id in ITEM_IDS:
            stock = 100
            for i, d in enumerate(DATES):
                usage = 10
                incoming = 20 if i == 2 else 0
                stock = stock - usage + incoming
                conn.execute(
                    "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty,"
                    " closing_stock) VALUES (?, ?, ?, ?, ?)",
                    (item_id, d, usage, incoming, stock),
                )
            conn.execute(
                "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
                " actual_date, actual_qty, status) VALUES (?, '2026-07-25', '2026-07-30', 20,"
                " '2026-07-30', 20, '입고 완료')",
                (item_id,),
            )
            conn.execute(
                "INSERT INTO incoming_shipments(item_id, order_date, expected_date, expected_qty,"
                " actual_date, actual_qty, status) VALUES (?, '2026-08-01', '2026-08-08', 30,"
                " NULL, NULL, '입고 예정')",
                (item_id,),
            )

        for i in range(8):
            conn.execute(
                "INSERT INTO action_history(item_id, action_type, risk_type, owner, note, status,"
                " risk_grade_before, risk_grade_after, result_note, created_at)"
                " VALUES (?, '부서 공유', ?, '김약사', '테스트 비고', '완료', '경고', '주의',"
                " '테스트 결과', ?)",
                (ITEM_IDS[i % len(ITEM_IDS)], RISK_TYPES_8[i], f"2026-0{i + 1}-01T09:00:00"),
            )

        meta_rows = [
            ("seed", "999"),
            ("base_date", "2026-08-01"),
            ("item_count", str(len(ITEM_IDS))),
            ("generated_at", "2026-08-01T09:30:00"),
            ("data_version", "1"),
            ("config_hash", "test-config-hash"),
        ]
        conn.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", meta_rows)
        conn.commit()

        content_hash = compute_content_hash(conn)
        conn.execute("INSERT INTO meta(key, value) VALUES ('content_hash', ?)", (content_hash,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def valid_db_path(tmp_path: Path) -> Path:
    path = tmp_path / "valid.db"
    build_valid_db(path)
    return path


@pytest.fixture()
def valid_conn(valid_db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(valid_db_path)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 섹션 A-1: 정상 DB에서 11항목 전부 PASS
# ---------------------------------------------------------------------------


class TestAllChecksPassOnValidDb:
    def test_run_all_checks_returns_eleven_results_all_pass(
        self, valid_conn: sqlite3.Connection
    ) -> None:
        results = vd.run_all_checks(valid_conn)
        assert len(results) == 11
        statuses = {name: result.status for name, result in results}
        assert all(status == "PASS" for status in statuses.values()), statuses

    def test_each_check_function_individually_passes(
        self, valid_conn: sqlite3.Connection
    ) -> None:
        assert vd.check_tables_and_meta(valid_conn).status == "PASS"
        assert vd.check_foreign_keys(valid_conn).status == "PASS"
        assert vd.check_stock_identity(valid_conn).status == "PASS"
        assert vd.check_non_negative(valid_conn).status == "PASS"
        assert vd.check_item_and_timeseries_counts(valid_conn).status == "PASS"
        assert vd.check_shipments(valid_conn).status == "PASS"
        assert vd.check_no_scenario_columns(valid_conn).status == "PASS"
        assert vd.check_pre_batch_tables_empty(valid_conn).status == "PASS"
        assert vd.check_action_history_seed(valid_conn).status == "PASS"
        assert vd.check_content_hash(valid_conn, None).status == "PASS"
        assert vd.check_arrival_ledger_consistency(valid_conn).status == "PASS"

    def test_content_hash_passes_with_matching_expect_hash(
        self, valid_conn: sqlite3.Connection
    ) -> None:
        recomputed = compute_content_hash(valid_conn)
        assert vd.check_content_hash(valid_conn, recomputed).status == "PASS"


# ---------------------------------------------------------------------------
# 섹션 A-2: 고의 훼손 케이스별 FAIL 검출
# ---------------------------------------------------------------------------


class TestCorruptionDetection:
    def test_stock_identity_violation_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE stock_usage_daily SET closing_stock = closing_stock + 5"
            " WHERE item_id = ? AND date = ?",
            (ITEM_IDS[0], DATES[2]),
        )
        conn.commit()
        result = vd.check_stock_identity(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_negative_stock_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE stock_usage_daily SET closing_stock = -1 WHERE item_id = ? AND date = ?",
            (ITEM_IDS[0], DATES[0]),
        )
        conn.commit()
        result = vd.check_non_negative(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_expected_qty_null_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE incoming_shipments SET expected_qty = NULL"
            " WHERE item_id = ? AND status = '입고 예정'",
            (ITEM_IDS[0],),
        )
        conn.commit()
        result = vd.check_shipments(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_ghost_income_fails_when_ledger_missing_arrived_qty(
        self, valid_db_path: Path
    ) -> None:
        """검사 11(Task S-22 픽스 라운드 1 F2, 컨트롤러 리뷰): incoming_shipments가 '도착
        완료'로 기록한 수량이 같은 (item_id, date)의 stock_usage_daily.incoming_qty에
        없으면(유령 입고) FAIL이어야 한다."""
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE stock_usage_daily SET incoming_qty = 0"
            " WHERE item_id = ? AND date = '2026-07-30'",
            (ITEM_IDS[0],),
        )
        conn.commit()
        result = vd.check_arrival_ledger_consistency(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_ghost_income_fails_when_arrival_date_outside_timeseries(
        self, valid_db_path: Path
    ) -> None:
        """도착 기록의 actual_date가 그 품목의 stock_usage_daily 범위 밖(행 자체가 없음)이면
        FAIL이어야 한다(재고 궤적에 반영될 자리조차 없다는 뜻)."""
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE incoming_shipments SET actual_date = '2099-01-01'"
            " WHERE item_id = ? AND status = '입고 완료'",
            (ITEM_IDS[0],),
        )
        conn.commit()
        result = vd.check_arrival_ledger_consistency(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_status_actual_date_mismatch_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE incoming_shipments SET actual_date = NULL, actual_qty = NULL"
            " WHERE item_id = ? AND status = '입고 완료'",
            (ITEM_IDS[0],),
        )
        conn.commit()
        result = vd.check_shipments(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_unknown_shipment_status_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "UPDATE incoming_shipments SET status = '알수없음'"
            " WHERE item_id = ? AND status = '입고 예정'",
            (ITEM_IDS[0],),
        )
        conn.commit()
        result = vd.check_shipments(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_meta_key_deleted_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute("DELETE FROM meta WHERE key = 'seed'")
        conn.commit()
        result = vd.check_tables_and_meta(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_missing_table_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute("DROP TABLE alerts")
        conn.commit()
        result = vd.check_tables_and_meta(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_hash_mismatch_against_expect_hash_fails(
        self, valid_conn: sqlite3.Connection
    ) -> None:
        wrong_hash = "0" * 64
        result = vd.check_content_hash(valid_conn, wrong_hash)
        assert result.status == "FAIL"

    def test_stale_stored_hash_fails_without_expect_hash(self, valid_db_path: Path) -> None:
        """meta.content_hash 갱신 없이 **원천 데이터**가 바뀌면 --expect-hash 없이도
        재계산 불일치 자체로 FAIL해야 한다.

        갱신 사유(S-17 리뷰 F1): 이 테스트는 원래 action_history 삽입을 '데이터 변경'
        프로브로 썼는데, action_history는 앱 사용 중 사람이 만드는 **런타임 기록**이라
        새 범위(부트스트랩 원천)에서 의도적으로 제외됐다. 단언을 약화하지 않고 프로브를
        원천 테이블(items)로 교체해 테스트의 원래 의도를 그대로 유지한다.
        """
        conn = sqlite3.connect(valid_db_path)
        conn.execute("UPDATE items SET item_name = item_name || '(변조)' WHERE item_id = ?",
                     (ITEM_IDS[0],))
        conn.commit()
        result = vd.check_content_hash(conn, None)
        conn.close()
        assert result.status == "FAIL"

    def test_runtime_table_changes_do_not_break_anchor(self, valid_db_path: Path) -> None:
        """파생·런타임 테이블이 바뀌어도 content_hash 앵커는 흔들리지 않는다(F1 룰링의 핵심).

        배치를 돌리거나(risk_results·forecasts) 앱을 쓰기만 해도(action_history·alerts)
        앵커가 깨지던 것이 F1의 결함이었다.
        """
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "INSERT INTO action_history(item_id, action_type, risk_type) VALUES (?, ?, ?)",
            (ITEM_IDS[0], "부서 공유", "general"),
        )
        conn.execute(
            "INSERT INTO alerts(alert_type, item_id, title, dedupe_key) VALUES (?, ?, ?, ?)",
            ("등급 변동", ITEM_IDS[0], "테스트 알림", "test-dedupe-1"),
        )
        conn.commit()
        result = vd.check_content_hash(conn, None)
        conn.close()
        assert result.status == "PASS"

    def test_action_history_seed_count_mismatch_fails(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "DELETE FROM action_history WHERE rowid ="
            " (SELECT rowid FROM action_history LIMIT 1)"
        )
        conn.commit()
        result = vd.check_action_history_seed(conn)
        conn.close()
        assert result.status == "FAIL"

    def test_scenario_named_column_fails(self, valid_db_path: Path) -> None:
        """결정 20(격리 뒷문 차단) 회귀 — 어떤 테이블이든 'scenario' 포함 컬럼이 생기면 검출."""
        conn = sqlite3.connect(valid_db_path)
        conn.execute("ALTER TABLE items ADD COLUMN linked_scenario_id TEXT")
        conn.commit()
        result = vd.check_no_scenario_columns(conn)
        conn.close()
        assert result.status == "FAIL"


# ---------------------------------------------------------------------------
# 섹션 A-3: WARN 경로(risk_results 등 비어있지 않아도 FAIL 아님)
# ---------------------------------------------------------------------------


class TestPreBatchWarnPath:
    def test_notices_excluded_from_pre_batch_empty_tables(self) -> None:
        """F8: notices는 M-11 이후 표준 빌드 시퀀스(generate_dataset → load_notices)의
        부트스트랩 계층이라, 배치 실행 전에도 20건이 정상이다 — "배치 전 공백" 검사
        대상에서 제외한다(검사 대상 목록 변경 자체를 명시 단언 — 단언 약화 금지)."""
        assert "notices" not in vd.PRE_BATCH_EMPTY_TABLES
        assert vd.PRE_BATCH_EMPTY_TABLES == ("risk_results", "forecasts", "alerts")

    def test_notices_non_empty_does_not_trigger_warn(self, valid_db_path: Path) -> None:
        """notices만 채워져 있고(부트스트랩 적재 상황 재현) risk_results/forecasts/alerts는
        비어 있는(배치 전) 표준 스냅샷 상태에서 check 8이 WARN 없이 PASS해야 한다."""
        conn = sqlite3.connect(valid_db_path)
        conn.executemany(
            "INSERT INTO notices(notice_id, published_date, title, notice_type)"
            " VALUES (?, '2026-07-01', ?, '공급중단')",
            [(f"NTC-{i:03d}", f"부트스트랩 공고 {i}") for i in range(20)],
        )
        new_hash = compute_content_hash(conn)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (new_hash,))
        conn.commit()

        result = vd.check_pre_batch_tables_empty(conn)
        conn.close()
        assert result.status == "PASS"

    def test_risk_results_non_empty_is_warn_not_fail(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute(
            "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, risk_type,"
            " factors_json) VALUES ('2026-08-01#test', ?, '2026-08-01', '정상', '정상',"
            " 'general', '{}')",
            (ITEM_IDS[0],),
        )
        # risk_results 적재 후에도 content_hash를 정상 유지한 스냅샷(배치가 정확히 재실행된
        # 상황)을 재현한다 — 그래야 이 테스트가 검증하려는 "8번만 WARN, 나머지는 그대로
        # PASS" 경로를 10번(content_hash)의 우연한 FAIL과 뒤섞지 않는다.
        new_hash = compute_content_hash(conn)
        conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (new_hash,))
        conn.commit()

        result = vd.check_pre_batch_tables_empty(conn)
        assert result.status == "WARN"

        all_results = vd.run_all_checks(conn)
        conn.close()
        assert not any(r.status == "FAIL" for _name, r in all_results)
        passed = sum(1 for _name, r in all_results if r.status != "FAIL")
        assert passed == 11


# ---------------------------------------------------------------------------
# 섹션 A-4: --expect-hash 파싱(원문 해시 / @파일경로)
# ---------------------------------------------------------------------------


class TestResolveExpectedHash:
    def test_plain_hash_returned_as_is(self) -> None:
        h = "a" * 64
        assert vd.resolve_expected_hash(h) == h

    def test_at_file_hash_line_after_comment(self, tmp_path: Path) -> None:
        h = "b" * 64
        path = tmp_path / "standard_snapshot.sha256"
        path.write_text(
            f"# python scripts/generate_dataset.py --seed 20260801\n{h}\n", encoding="utf-8"
        )
        assert vd.resolve_expected_hash(f"@{path}") == h

    def test_at_file_hash_line_before_comment(self, tmp_path: Path) -> None:
        h = "c" * 64
        path = tmp_path / "standard_snapshot2.sha256"
        path.write_text(f"{h}\n# generated by ...\n", encoding="utf-8")
        assert vd.resolve_expected_hash(f"@{path}") == h


# ---------------------------------------------------------------------------
# 섹션 A-5: exit code(subprocess 실행)
# ---------------------------------------------------------------------------


class TestCliExitCode:
    def test_valid_db_exits_zero_and_prints_passed(self, valid_db_path: Path) -> None:
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "--db", str(valid_db_path)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "VALIDATION PASSED (11/11)" in proc.stdout

    def test_corrupted_db_exits_one_and_prints_failed(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        conn.execute("DELETE FROM meta WHERE key = 'seed'")
        conn.commit()
        conn.close()

        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "--db", str(valid_db_path)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 1
        assert "VALIDATION FAILED" in proc.stdout

    def test_expect_hash_mismatch_via_cli_exits_one(self, valid_db_path: Path) -> None:
        proc = subprocess.run(
            [
                sys.executable, str(VALIDATE_SCRIPT), "--db", str(valid_db_path),
                "--expect-hash", "f" * 64,
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 1
        assert "VALIDATION FAILED" in proc.stdout

    def test_expect_hash_match_via_cli_exits_zero(self, valid_db_path: Path) -> None:
        conn = sqlite3.connect(valid_db_path)
        stored = conn.execute(
            "SELECT value FROM meta WHERE key = 'content_hash'"
        ).fetchone()[0]
        conn.close()

        proc = subprocess.run(
            [
                sys.executable, str(VALIDATE_SCRIPT), "--db", str(valid_db_path),
                "--expect-hash", stored,
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 0
        assert "VALIDATION PASSED (11/11)" in proc.stdout

    def test_missing_db_file_exits_one(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.db"
        proc = subprocess.run(
            [sys.executable, str(VALIDATE_SCRIPT), "--db", str(missing)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 1


# ---------------------------------------------------------------------------
# 섹션 B: scripts/generate_dataset.py 이력 시드 통합
# ---------------------------------------------------------------------------


class TestApplyHistorySeed:
    """apply_history_seed()는 generate_baseline/inject_scenarios가 끝난 뒤에만 호출
    가능하다(두 함수 모두 out_path를 삭제 후 재생성하므로). skip=False면 8건을 적재하고
    content_hash를 재계산·갱신, skip=True면 아무 것도 하지 않는다."""

    def test_loads_eight_rows_and_keeps_hash_self_consistent(self, tmp_path: Path) -> None:
        """이력 시드 8건을 적재하고, 저장된 content_hash가 재계산 값과 계속 일치한다.

        갱신 사유(S-17 리뷰 F1): 원래 이 테스트는 ``new_hash != pre_hash``를 단언했다.
        action_history가 새 범위(부트스트랩 원천)에서 제외됐으므로, 시드 적재는 앵커를
        **바꾸지 않는 것이 정상**이다. 단언을 삭제해 약화하는 대신 **새 의미론의 등식으로
        교체**한다(pre == new == stored == recomputed) — 자기정합이라는 원래 검증 의도는
        그대로 유지되고, 오히려 "런타임 데이터가 앵커를 흔들지 않는다"는 F1 계약까지 함께
        고정된다.
        """
        out = tmp_path / "medsupply.db"
        generate_baseline(out, seed=999, base_date="2026-08-01")
        conn = sqlite3.connect(out)
        pre_hash = conn.execute(
            "SELECT value FROM meta WHERE key = 'content_hash'"
        ).fetchone()[0]
        conn.close()

        count, new_hash = gen_cli.apply_history_seed(
            out, csv_path=ACTION_HISTORY_SEED_CSV, skip=False
        )

        assert count == 8
        assert new_hash is not None
        assert new_hash == pre_hash

        conn = sqlite3.connect(out)
        try:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
            recomputed = compute_content_hash(conn)
            action_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        finally:
            conn.close()
        assert stored == new_hash == recomputed
        assert action_count == 8

    def test_skip_does_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "medsupply.db"
        generate_baseline(out, seed=999, base_date="2026-08-01")
        conn = sqlite3.connect(out)
        pre_hash = conn.execute(
            "SELECT value FROM meta WHERE key = 'content_hash'"
        ).fetchone()[0]
        conn.close()

        count, new_hash = gen_cli.apply_history_seed(
            out, csv_path=ACTION_HISTORY_SEED_CSV, skip=True
        )

        assert count == 0
        assert new_hash is None

        conn = sqlite3.connect(out)
        try:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
            action_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        finally:
            conn.close()
        assert stored == pre_hash
        assert action_count == 0


class TestGenerateDatasetCliHistorySeedFlag:
    """main()이 주입 경로(비-baseline-only)에서 이력 시드를 기본 적재하고 --skip-history-seed로
    옵트아웃할 수 있는지, 종료 요약에 시드 건수가 찍히는지 종단 검증한다."""

    def test_default_loads_seed_and_prints_count(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "medsupply.db"
        summary = gen_cli.main(
            ["--out", str(out), "--seed", str(SEED_A), "--base-date", BASE_DATE]
        )
        captured = capsys.readouterr()
        assert "이력 시드 건수: 8" in captured.out

        conn = sqlite3.connect(out)
        try:
            action_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
            stored_hash = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
            recomputed = compute_content_hash(conn)
        finally:
            conn.close()
        assert action_count == 8
        assert stored_hash == recomputed == summary.content_hash

    def test_skip_flag_leaves_action_history_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out = tmp_path / "medsupply.db"
        summary = gen_cli.main(
            [
                "--out", str(out), "--seed", str(SEED_A), "--base-date", BASE_DATE,
                "--skip-history-seed",
            ]
        )
        captured = capsys.readouterr()
        assert "이력 시드 건수: 0" in captured.out

        conn = sqlite3.connect(out)
        try:
            action_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
            stored_hash = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert action_count == 0
        assert stored_hash == summary.content_hash

    def test_baseline_only_never_loads_history_seed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--baseline-only는 '전체 빌드'가 아니므로 이력 시드를 적재하지 않는다(브리프:
        전체 빌드 시 적재 — 절차 1단계 명령은 --baseline-only를 쓰지 않는다)."""
        out = tmp_path / "medsupply.db"
        gen_cli.main(
            [
                "--out", str(out), "--seed", str(SEED_A), "--base-date", BASE_DATE,
                "--baseline-only",
            ]
        )
        captured = capsys.readouterr()
        assert "이력 시드 건수: 0" in captured.out

        conn = sqlite3.connect(out)
        try:
            action_count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
        finally:
            conn.close()
        assert action_count == 0
