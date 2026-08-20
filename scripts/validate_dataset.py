"""표준 스냅샷 독립 정합성 검증기.

scripts/generate_dataset.py가 만든 SQLite 스냅샷(예: data/medsupply.db)이 데이터 계약을
지키는지 기계적으로 검증한다. 10개 항목을 각각 PASS/WARN/FAIL로 판정해 한 줄씩 출력하고,
FAIL이 하나라도 있으면 종료 코드 1을 반환한다. WARN은 실패로 치지 않는다(risk_results 등
배치·적재 이후 재실행된 스냅샷을 검증할 가능성을 배려한 경고일 뿐이다).

사용법:
    python scripts/validate_dataset.py --db data/medsupply.db
    python scripts/validate_dataset.py --db data/medsupply.db --expect-hash <sha256>
    python scripts/validate_dataset.py --db data/medsupply.db \
        --expect-hash @data/scenarios/standard_snapshot.sha256

--expect-hash는 64자리 sha256 hex를 직접 받거나, '@경로' 형태면 그 파일의 첫 번째
비-주석(#으로 시작하지 않는) 줄을 해시 값으로 읽는다(data/scenarios/standard_snapshot.sha256
포맷: 생성 명령 주석 1줄 + content_hash 1줄).

**구현 위치·격리 원칙**: 이 모듈은 scripts/ 직속이다(scripts/datagen/ 밖) — 격리 대상인
시나리오/ground truth를 전혀 참조하지 않는 순수 스냅샷 정합성 검사이기 때문이다.
`medsupply` 패키지는 일절 import하지 않는다(sqlite3 직접 사용). content_hash 재계산만
scripts.datagen.baseline.compute_content_hash를 재사용한다(scripts.datagen 내부 재사용은
허용 — generate_dataset.py와 동일한 sys.path 처리 방식을 따른다).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.*`를 절대 경로 실행에서도 import할 수
# 있게 한다(generate_dataset.py와 동일한 방식).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen.baseline import compute_content_hash  # noqa: E402

# ---------------------------------------------------------------------------
# 계약 상수 — 마스터 플랜 결정 11(16개 테이블), 결정 12/22(meta 7키)
# ---------------------------------------------------------------------------

#: 결정 11: 16개 테이블 한 벌.
EXPECTED_TABLES: tuple[str, ...] = (
    "ingredients", "ingredient_aliases", "substitute_groups", "items",
    "stock_usage_daily", "incoming_shipments",
    "notices", "notice_extractions", "notice_item_map",
    "risk_results", "forecasts", "llm_explanations",
    "alerts", "action_history", "order_requests", "meta",
)

#: 결정 12: meta 단일 키-값 테이블의 7개 키.
EXPECTED_META_KEYS: tuple[str, ...] = (
    "seed", "config_hash", "content_hash", "base_date", "item_count", "data_version",
    "generated_at",
)

#: incoming_shipments.status 허용 값(생성기가 실제로 쓰는 값 — schema.sql은 CHECK 없이
#: "값 집합은 데이터 생성 태스크가 확정"으로 위임했다. 이 검증기가 그 확정을 집행한다).
VALID_SHIPMENT_STATUSES: frozenset[str] = frozenset({"입고 완료", "입고 예정"})

#: action_history.risk_type 허용 값(schema.sql CHECK와 동일한 5종. NULL은 별도 허용).
VALID_RISK_TYPES: frozenset[str] = frozenset(
    {"demand_surge", "supply_halt", "delivery_delay", "composite", "general"}
)

#: 배치·LLM 적재 이전 스냅샷에서는 비어 있어야 정상인 테이블(브리프 검사 항목 8).
#: notices는 여기 포함하지 않는다 — M-11(scripts/load_notices.py)부터 "표준 스냅샷"의
#: 정의 자체가 "데이터셋 생성 + 공고 적재 완료" 상태로 확장되어, 위험 평가 배치가 실행되기
#: 전에도 20건이 적재돼 있는 것이 정상(부트스트랩 계층)이다 — risk_results·forecasts·
#: alerts처럼 "배치가 이미 돌았다"를 뜻하는 신호가 아니다(2주차 브랜치 리뷰 F8).
PRE_BATCH_EMPTY_TABLES: tuple[str, ...] = ("risk_results", "forecasts", "alerts")

#: action_history_seed.csv 시드 건수(S-12 산출물 고정값, 브리프 검사 항목 9).
EXPECTED_ACTION_HISTORY_SEED_COUNT = 8


@dataclass(frozen=True)
class CheckResult:
    """검사 1건의 판정. status는 'PASS' | 'WARN' | 'FAIL' 중 하나."""

    status: str
    message: str


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }


# ---------------------------------------------------------------------------
# 1. 테이블 16종 + meta 7키 완비성
# ---------------------------------------------------------------------------


def check_tables_and_meta(conn: sqlite3.Connection) -> CheckResult:
    tables = _table_names(conn)
    missing_tables = sorted(set(EXPECTED_TABLES) - tables)
    if missing_tables:
        return CheckResult("FAIL", f"누락된 테이블: {missing_tables}")

    meta_keys = {row[0] for row in conn.execute("SELECT key FROM meta")}
    missing_keys = sorted(set(EXPECTED_META_KEYS) - meta_keys)
    if missing_keys:
        return CheckResult("FAIL", f"누락된 meta 키: {missing_keys}")

    return CheckResult(
        "PASS", f"테이블 {len(EXPECTED_TABLES)}종·meta {len(EXPECTED_META_KEYS)}키 확인"
    )


# ---------------------------------------------------------------------------
# 2. FK 정합
# ---------------------------------------------------------------------------


def check_foreign_keys(conn: sqlite3.Connection) -> CheckResult:
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        return CheckResult("FAIL", f"FK 위반 {len(violations)}건")
    return CheckResult("PASS", "FK 위반 0건")


# ---------------------------------------------------------------------------
# 3. 재고 항등식(전수) — closing[t-1] - usage[t] + incoming[t] == closing[t]
# ---------------------------------------------------------------------------


def check_stock_identity(conn: sqlite3.Connection) -> CheckResult:
    """품목별 날짜순으로 순회하며 연속한 두 행의 재고 항등식을 전수 검사한다.

    각 품목의 첫 행(t=0)은 비교 대상인 t-1 행이 DB에 존재하지 않으므로(초기 재고는 별도
    행으로 저장되지 않는다) 검사에서 자연히 제외된다 — 이는 데이터 생성기의 설계이지
    검증기의 허점이 아니다.
    """
    rows = conn.execute(
        "SELECT item_id, date, usage_qty, incoming_qty, closing_stock"
        " FROM stock_usage_daily ORDER BY item_id, date"
    ).fetchall()

    checked = 0
    violations: list[str] = []
    prev_item: str | None = None
    prev_closing: int | None = None
    for item_id, d, usage, incoming, closing in rows:
        if item_id == prev_item:
            checked += 1
            if prev_closing - usage + incoming != closing:
                violations.append(f"{item_id}@{d}")
        prev_item = item_id
        prev_closing = closing

    if violations:
        sample = violations[:5]
        return CheckResult(
            "FAIL", f"항등식 위반 {len(violations)}건(검사 {checked}건 중), 예: {sample}"
        )
    return CheckResult("PASS", f"항등식 일치 {checked}건(전수)")


# ---------------------------------------------------------------------------
# 4. usage_qty · closing_stock 비음수
# ---------------------------------------------------------------------------


def check_non_negative(conn: sqlite3.Connection) -> CheckResult:
    total = conn.execute("SELECT COUNT(*) FROM stock_usage_daily").fetchone()[0]
    bad = conn.execute(
        "SELECT COUNT(*) FROM stock_usage_daily WHERE usage_qty < 0 OR closing_stock < 0"
    ).fetchone()[0]
    if bad:
        return CheckResult("FAIL", f"음수 usage_qty/closing_stock {bad}건 / 전체 {total}건")
    return CheckResult("PASS", f"전체 {total}건 음수 없음")


# ---------------------------------------------------------------------------
# 5. 품목 수 == items 행수 == meta.item_count, 시계열 행수 == 품목수×기간
# ---------------------------------------------------------------------------


def check_item_and_timeseries_counts(conn: sqlite3.Connection) -> CheckResult:
    item_count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    meta_row = conn.execute("SELECT value FROM meta WHERE key = 'item_count'").fetchone()
    if meta_row is None:
        return CheckResult("FAIL", "meta.item_count 없음")
    try:
        meta_item_count = int(meta_row[0])
    except ValueError:
        return CheckResult("FAIL", f"meta.item_count 정수 아님: {meta_row[0]!r}")

    if item_count != meta_item_count:
        return CheckResult(
            "FAIL", f"items 행수({item_count}) != meta.item_count({meta_item_count})"
        )

    distinct_dates = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM stock_usage_daily"
    ).fetchone()[0]
    ts_count = conn.execute("SELECT COUNT(*) FROM stock_usage_daily").fetchone()[0]
    expected_ts = item_count * distinct_dates
    if ts_count != expected_ts:
        return CheckResult(
            "FAIL",
            f"시계열 행수({ts_count}) != 품목수×기간"
            f"({item_count}×{distinct_dates}={expected_ts})",
        )

    return CheckResult(
        "PASS",
        f"품목 {item_count}개 = meta.item_count, 시계열 {ts_count}행 ="
        f" {item_count}×{distinct_dates}일",
    )


# ---------------------------------------------------------------------------
# 6. incoming_shipments 정합 — status 값 집합·완료↔actual·expected_qty NOT NULL
# ---------------------------------------------------------------------------


def check_shipments(conn: sqlite3.Connection) -> CheckResult:
    rows = conn.execute(
        "SELECT status, actual_date, expected_qty FROM incoming_shipments"
    ).fetchall()
    total = len(rows)

    statuses = {r[0] for r in rows}
    unknown_statuses = sorted(statuses - VALID_SHIPMENT_STATUSES)
    if unknown_statuses:
        return CheckResult("FAIL", f"알 수 없는 status 값: {unknown_statuses}")

    mismatched = sum(
        1
        for status, actual_date, _expected_qty in rows
        if (status == "입고 완료") != (actual_date is not None)
    )
    if mismatched:
        return CheckResult("FAIL", f"'입고 완료'↔actual_date 정합 위반 {mismatched}건")

    null_expected = sum(
        1 for _status, _actual_date, expected_qty in rows if expected_qty is None
    )
    if null_expected:
        return CheckResult("FAIL", f"expected_qty NULL {null_expected}건")

    return CheckResult("PASS", f"전체 {total}건 status/정합/expected_qty 이상 없음")


# ---------------------------------------------------------------------------
# 7. 격리 뒷문 검사(결정 20) — 'scenario' 포함 컬럼명 없음
# ---------------------------------------------------------------------------


def check_no_scenario_columns(conn: sqlite3.Connection) -> CheckResult:
    offenders: list[str] = []
    for table in sorted(_table_names(conn)):
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        for col in columns:
            col_name = col[1]
            if "scenario" in col_name.lower():
                offenders.append(f"{table}.{col_name}")
    if offenders:
        return CheckResult("FAIL", f"'scenario' 포함 컬럼 발견(격리 뒷문): {offenders}")
    return CheckResult("PASS", "'scenario' 포함 컬럼 없음")


# ---------------------------------------------------------------------------
# 8. 배치/적재 전 공백 테이블 — 비어있지 않으면 WARN(FAIL 아님)
# ---------------------------------------------------------------------------


def check_pre_batch_tables_empty(conn: sqlite3.Connection) -> CheckResult:
    non_empty: dict[str, int] = {}
    for table in PRE_BATCH_EMPTY_TABLES:
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        if count:
            non_empty[table] = count
    if non_empty:
        return CheckResult(
            "WARN",
            f"비어있지 않음(배치·적재 이후 재실행된 스냅샷일 가능성, FAIL 아님): {non_empty}",
        )
    return CheckResult("PASS", f"{', '.join(PRE_BATCH_EMPTY_TABLES)} 전부 비어 있음")


# ---------------------------------------------------------------------------
# 9. action_history == 시드 8건(risk_type 값 집합 검증)
# ---------------------------------------------------------------------------


def check_action_history_seed(conn: sqlite3.Connection) -> CheckResult:
    count = conn.execute("SELECT COUNT(*) FROM action_history").fetchone()[0]
    if count != EXPECTED_ACTION_HISTORY_SEED_COUNT:
        return CheckResult(
            "FAIL",
            f"action_history 행수 {count} != 기대값 {EXPECTED_ACTION_HISTORY_SEED_COUNT}",
        )

    risk_types = {
        row[0] for row in conn.execute("SELECT DISTINCT risk_type FROM action_history")
    }
    invalid = sorted(rt for rt in risk_types if rt is not None and rt not in VALID_RISK_TYPES)
    if invalid:
        return CheckResult("FAIL", f"알 수 없는 risk_type: {invalid}")

    return CheckResult("PASS", f"action_history {count}건, risk_type 값 집합 정상")


# ---------------------------------------------------------------------------
# 10. content_hash 재계산 == meta.content_hash (+ --expect-hash 일치)
# ---------------------------------------------------------------------------


def check_content_hash(conn: sqlite3.Connection, expect_hash: str | None) -> CheckResult:
    recomputed = compute_content_hash(conn)

    stored_row = conn.execute("SELECT value FROM meta WHERE key = 'content_hash'").fetchone()
    if stored_row is None:
        return CheckResult("FAIL", "meta.content_hash 없음")
    stored = stored_row[0]

    if recomputed != stored:
        return CheckResult(
            "FAIL",
            f"재계산 해시({recomputed[:12]}...) != meta.content_hash({stored[:12]}...)",
        )

    if expect_hash is not None and recomputed != expect_hash:
        return CheckResult(
            "FAIL",
            f"재계산 해시({recomputed[:12]}...)가 --expect-hash({expect_hash[:12]}...)와 불일치",
        )

    suffix = " (+ --expect-hash 일치)" if expect_hash is not None else ""
    return CheckResult("PASS", f"재계산 해시가 meta.content_hash와 일치{suffix}: {recomputed}")


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


def _safe(name: str, fn, *args: object) -> tuple[str, CheckResult]:
    """검사 함수 실행 중 sqlite3 오류(예: 테이블 자체가 없어 다른 검사가 연쇄로 깨지는
    경우)가 나도 전체 스크립트가 죽지 않고 해당 항목만 FAIL로 보고하게 한다."""
    try:
        return name, fn(*args)
    except sqlite3.Error as exc:
        return name, CheckResult("FAIL", f"쿼리 오류: {exc}")


def run_all_checks(
    conn: sqlite3.Connection, expect_hash: str | None = None
) -> list[tuple[str, CheckResult]]:
    """10개 검사를 브리프 순서대로 실행해 (이름, 결과) 리스트를 반환한다."""
    return [
        _safe("1. 테이블 16종/meta 7키 완비성", check_tables_and_meta, conn),
        _safe("2. FK 정합(foreign_key_check)", check_foreign_keys, conn),
        _safe("3. 재고 항등식(전수)", check_stock_identity, conn),
        _safe("4. usage_qty/closing_stock 비음수", check_non_negative, conn),
        _safe("5. 품목수/시계열행수 정합", check_item_and_timeseries_counts, conn),
        _safe("6. incoming_shipments 정합", check_shipments, conn),
        _safe("7. 격리 뒷문 검사(scenario 컬럼)", check_no_scenario_columns, conn),
        _safe("8. 배치 전 공백 테이블(WARN 허용)", check_pre_batch_tables_empty, conn),
        _safe("9. action_history 시드 8건", check_action_history_seed, conn),
        _safe("10. content_hash 재계산 일치", check_content_hash, conn, expect_hash),
    ]


def resolve_expected_hash(value: str) -> str:
    """--expect-hash 값을 해석한다.

    '@경로' 형태면 그 파일에서 첫 번째 비어있지 않은, '#'으로 시작하지 않는 줄을 해시 값으로
    읽는다(data/scenarios/standard_snapshot.sha256의 "생성 명령 주석 + content_hash 1줄"
    포맷을 순서 무관하게 파싱). 그 외에는 입력값을 그대로(양끝 공백만 제거) 반환한다.
    """
    if value.startswith("@"):
        path = Path(value[1:])
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                return stripped
        raise ValueError(f"{path}에서 해시 값을 찾을 수 없다(주석이 아닌 줄이 없음)")
    return value.strip()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 표준 스냅샷 독립 정합성 검증기"
    )
    parser.add_argument("--db", required=True, help="검증할 SQLite DB 경로")
    parser.add_argument(
        "--expect-hash",
        default=None,
        help=(
            "기대 content_hash(sha256 hex) 또는 '@파일경로'"
            "(예: @data/scenarios/standard_snapshot.sha256)"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[FAIL] DB 파일이 존재하지 않는다: {db_path}")
        print("VALIDATION FAILED (0/10)")
        return 1

    expect_hash = resolve_expected_hash(args.expect_hash) if args.expect_hash else None

    conn = sqlite3.connect(db_path)
    try:
        results = run_all_checks(conn, expect_hash)
    finally:
        conn.close()

    for name, result in results:
        print(f"[{result.status}] {name}: {result.message}")

    total = len(results)
    has_fail = any(result.status == "FAIL" for _name, result in results)
    passed = sum(1 for _name, result in results if result.status != "FAIL")

    if has_fail:
        print(f"VALIDATION FAILED ({passed}/{total})")
        return 1

    print(f"VALIDATION PASSED ({passed}/{total})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
