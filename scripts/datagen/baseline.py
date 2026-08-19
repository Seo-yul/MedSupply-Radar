"""결정적 베이스라인 데이터 생성 엔진 — 정상 운영 패턴 12개월 일별 시계열.

**격리 원칙**: 이 모듈은 `medsupply` 패키지를 일절 import하지 않는다(분석 로직과의 격리).
스키마는 `medsupply/data/schema.sql` 파일을 직접 읽어 executescript로 적용한다. 시나리오
config(`data/scenarios/scenario_config.yaml`, `scripts/datagen/config.py`)는 이 모듈에서
읽지 않는다 — 이 모듈은 `--baseline-only` 경로만 구현한다(시나리오 주입은 S-12).

**완전 결정성**: 같은 (seed, base_date, 마스터 CSV) → 바이트 동일한 데이터. 난수는
`random.Random(서브시드)`만 사용한다 — 전역 `random`, `numpy.random`, `datetime.now()`는
어디에서도 쓰지 않는다. 품목별 서브시드는 `item_subseed()`로, 공급사별 리드타임은
`supplier_lead_time()`으로 결정한다(둘 다 해시 기반, 순수 함수).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import random
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"
DEFAULT_REFERENCE_DIR = REPO_ROOT / "data" / "reference"

# ---------------------------------------------------------------------------
# 품목 프로파일 상수 (브리프 §생성 규칙)
# ---------------------------------------------------------------------------

#: 제형 → 평시 일수요 uniform 범위 버킷.
_FORM_BUCKETS: dict[str, str] = {
    "정제": "tablet_capsule",
    "캡슐": "tablet_capsule",
    "시럽": "syrup",
    "주사": "injection_vial",
    "바이알": "injection_vial",
    "서방정": "extended_release",
    "산제": "powder_patch",
    "패치": "powder_patch",
}

_BASE_USAGE_RANGES: dict[str, tuple[float, float]] = {
    "tablet_capsule": (8.0, 60.0),
    "syrup": (3.0, 20.0),
    "injection_vial": (2.0, 15.0),
    "extended_release": (5.0, 30.0),
    "powder_patch": (2.0, 10.0),
}

#: 계절성 대상 ATC — N02(해열진통)·J01(항생제)는 정확히 3자리 일치, R계열(호흡기)은 접두어.
_SEASONAL_ATC_PREFIXES: tuple[str, ...] = ("N02", "J01", "R")

_WINTER_MONTHS = (12, 1, 2)
_SUMMER_MONTHS = (6, 7, 8)


def _form_bucket(form: str) -> str:
    try:
        return _FORM_BUCKETS[form]
    except KeyError as exc:
        raise ValueError(f"알 수 없는 제형: {form!r}") from exc


def item_subseed(seed: int, item_id: str) -> int:
    """품목별 서브시드 = sha256(f"{seed}:{item_id}") 앞 8자리 hex를 정수로."""
    digest = hashlib.sha256(f"{seed}:{item_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def supplier_lead_time(supplier: str) -> int:
    """공급사별 고정 리드타임(3~7일). 공급사명 해시로 결정(전역 seed와 무관)."""
    digest = hashlib.sha256(supplier.encode("utf-8")).hexdigest()
    return 3 + (int(digest[:8], 16) % 5)


def _weekly_factor(d: date) -> float:
    """평일 1.0, 토 0.6, 일 0.45 (병원 외래 리듬). weekday(): 월=0 ... 일=6."""
    weekday = d.weekday()
    if weekday == 5:
        return 0.6
    if weekday == 6:
        return 0.45
    return 1.0


def _is_seasonal_atc(atc_code: str) -> bool:
    return atc_code.startswith(_SEASONAL_ATC_PREFIXES)


def _seasonal_factor(d: date, atc_code: str) -> float:
    """N02·J01·R계열만 12~2월 ×1.35, 6~8월 ×0.85. 그 외 품목·월은 계절성 없음(1.0)."""
    if not _is_seasonal_atc(atc_code):
        return 1.0
    if d.month in _WINTER_MONTHS:
        return 1.35
    if d.month in _SUMMER_MONTHS:
        return 0.85
    return 1.0


def _round_to_pack(qty: float, pack_size: int) -> int:
    """가장 가까운 pack_size 배수로 반올림. 최소 1 pack(0으로 내려가지 않음).

    브리프의 "pack_size 배수로 반올림"을 그대로 적용하면 소량 품목(예: 산제,
    base_usage~2, pack_size=100)에서 초기 재고가 0으로 반올림될 수 있다 — 이는
    "정상 운영" 베이스라인의 전제(초기 재고 > 0)에 어긋나므로 최소 1 pack으로 방어한다.
    """
    multiples = max(1, round(qty / pack_size))
    return multiples * pack_size


def _ceil_to_pack(qty: float, pack_size: int) -> int:
    """pack_size 배수로 올림. 최소 1 pack."""
    multiples = max(1, math.ceil(qty / pack_size))
    return multiples * pack_size


# ---------------------------------------------------------------------------
# 품목별 재고·사용량·발주 시뮬레이션
# ---------------------------------------------------------------------------


def _simulate_item(
    item: dict[str, str], seed: int, days: list[date]
) -> tuple[list[dict[str, object]], list[dict[str, object]], int]:
    """단일 품목의 일별 재고·사용량·발주 시뮬레이션(날짜 순 진행).

    반환: (stock_rows, shipment_rows, truncation_count).
    stock_rows: stock_usage_daily INSERT용 dict 리스트(item_id, date, usage_qty,
    incoming_qty, closing_stock). shipment_rows: incoming_shipments INSERT용 dict
    리스트(item_id, order_date, expected_date, expected_qty, actual_date, actual_qty,
    status). truncation_count: 재고 부족으로 usage가 절삭된 일수(정상은 0).
    """
    item_id = item["item_id"]
    pack_size = int(item["pack_size"])
    supplier = item["supplier"]
    atc_code = item["atc_code"]

    rng = random.Random(item_subseed(seed, item_id))

    lo, hi = _BASE_USAGE_RANGES[_form_bucket(item["form"])]
    base_usage = round(rng.uniform(lo, hi), 1)

    lead_time = supplier_lead_time(supplier)
    rop = base_usage * (lead_time + 7)
    reorder_qty = _ceil_to_pack(base_usage * 30, pack_size)

    initial_days = rng.randint(20, 35)
    stock = _round_to_pack(base_usage * initial_days, pack_size)

    stock_rows: list[dict[str, object]] = []
    shipment_rows: list[dict[str, object]] = []
    pending_row: dict[str, object] | None = None
    pending_expected: date | None = None
    truncation_count = 0

    for d in days:
        noise = max(0.5, rng.gauss(1.0, 0.12))
        raw_usage = round(base_usage * _weekly_factor(d) * _seasonal_factor(d, atc_code) * noise)
        raw_usage = max(0, raw_usage)

        if raw_usage > stock:
            usage = stock
            truncation_count += 1
        else:
            usage = raw_usage

        incoming = 0
        if pending_row is not None and pending_expected == d:
            incoming = int(pending_row["expected_qty"])  # type: ignore[arg-type]
            pending_row["actual_date"] = d.isoformat()
            pending_row["actual_qty"] = incoming
            pending_row["status"] = "입고 완료"
            pending_row = None
            pending_expected = None

        stock = stock - usage + incoming

        stock_rows.append(
            {
                "item_id": item_id,
                "date": d.isoformat(),
                "usage_qty": usage,
                "incoming_qty": incoming,
                "closing_stock": stock,
            }
        )

        if stock < rop and pending_row is None:
            expected_date = d + timedelta(days=lead_time)
            new_row: dict[str, object] = {
                "item_id": item_id,
                "order_date": d.isoformat(),
                "expected_date": expected_date.isoformat(),
                "expected_qty": reorder_qty,
                "actual_date": None,
                "actual_qty": None,
                "status": "입고 예정",
            }
            shipment_rows.append(new_row)
            pending_row = new_row
            pending_expected = expected_date

    return stock_rows, shipment_rows, truncation_count


# ---------------------------------------------------------------------------
# 기준정보 CSV 적재
# ---------------------------------------------------------------------------


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def apply_schema(conn: sqlite3.Connection, schema_path: Path = DEFAULT_SCHEMA_PATH) -> None:
    """schema.sql 파일을 직접 읽어 executescript로 적용한다(medsupply import 없음)."""
    schema_sql = schema_path.read_text(encoding="utf-8")
    conn.executescript(schema_sql)


def _insert_ingredients(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        "INSERT INTO ingredients (ingredient_code, ingredient_name_kr, ingredient_name_en,"
        " atc_code) VALUES (:ingredient_code, :ingredient_name_kr, :ingredient_name_en,"
        " :atc_code)",
        rows,
    )


def _insert_substitute_groups(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        "INSERT INTO substitute_groups (substitute_group_id, ingredient_code, strength,"
        " form, route, group_label) VALUES (:substitute_group_id, :ingredient_code,"
        " :strength, :form, :route, :group_label)",
        rows,
    )


def _insert_items(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    processed = [
        {**row, "pack_size": int(row["pack_size"]), "is_essential": int(row["is_essential"])}
        for row in rows
    ]
    conn.executemany(
        "INSERT INTO items (item_id, item_name, standard_code, ingredient_code, strength,"
        " form, route, pack_size, supplier, is_essential, substitute_group_id, atc_code)"
        " VALUES (:item_id, :item_name, :standard_code, :ingredient_code, :strength, :form,"
        " :route, :pack_size, :supplier, :is_essential, :substitute_group_id, :atc_code)",
        processed,
    )


def _insert_ingredient_aliases(conn: sqlite3.Connection, rows: list[dict[str, str]]) -> None:
    conn.executemany(
        "INSERT INTO ingredient_aliases (alias, ingredient_code, alias_type)"
        " VALUES (:alias, :ingredient_code, :alias_type)",
        rows,
    )


# ---------------------------------------------------------------------------
# content_hash — 전 테이블(meta.content_hash 자신 제외)을 테이블명·PK 순 정렬 직렬화
# ---------------------------------------------------------------------------


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # PRAGMA table_info 컬럼: (cid, name, type, notnull, dflt_value, pk)
    pk_pairs = [(row[5], row[1]) for row in rows if row[5]]
    pk_pairs.sort(key=lambda pair: pair[0])
    return [name for _, name in pk_pairs]


def _all_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name"
    ).fetchall()
    return [row[0] for row in rows]


def compute_content_hash(conn: sqlite3.Connection) -> str:
    parts: list[str] = []
    for table in _all_table_names(conn):
        pk_cols = _pk_columns(conn, table)
        order_clause = ", ".join(pk_cols) if pk_cols else "rowid"
        cur = conn.execute(f"SELECT * FROM {table} ORDER BY {order_clause}")
        col_names = [d[0] for d in cur.description]
        key_idx = col_names.index("key") if table == "meta" else None
        for row in cur.fetchall():
            if key_idx is not None and row[key_idx] == "content_hash":
                continue
            values = ["\x00" if v is None else str(v) for v in row]
            parts.append(table + "\x1f" + "\x1f".join(values))
    serialized = "\x1e".join(parts)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationSummary:
    """생성 요약 — CLI 출력 및 테스트 검증에 쓰인다."""

    item_count: int
    timeseries_row_count: int
    shipment_count: int
    truncation_count: int
    content_hash: str
    elapsed_seconds: float


def generate_baseline(
    out_path: str | Path,
    seed: int,
    base_date: str | date,
    *,
    reference_dir: str | Path = DEFAULT_REFERENCE_DIR,
    schema_path: str | Path = DEFAULT_SCHEMA_PATH,
) -> GenerationSummary:
    """베이스라인(정상 운영 패턴) SQLite 스냅샷을 out_path에 결정적으로 생성한다.

    기존 out_path 파일이 있으면 삭제 후 재생성한다. 시나리오 config는 읽지 않는다
    (--baseline-only 전용 경로).
    """
    start = time.monotonic()

    out_path = Path(out_path)
    reference_dir = Path(reference_dir)
    schema_path = Path(schema_path)
    base_date_obj = date.fromisoformat(base_date) if isinstance(base_date, str) else base_date
    timeline_start = base_date_obj - timedelta(days=364)
    days = [timeline_start + timedelta(days=i) for i in range(365)]

    if out_path.exists():
        out_path.unlink()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(out_path)
    try:
        apply_schema(conn, schema_path)

        ingredients = _load_csv(reference_dir / "ingredients.csv")
        groups = _load_csv(reference_dir / "substitute_groups.csv")
        items = _load_csv(reference_dir / "items_master.csv")
        aliases_path = reference_dir / "ingredient_aliases.csv"
        aliases = _load_csv(aliases_path) if aliases_path.exists() else []

        with conn:
            _insert_ingredients(conn, ingredients)
            _insert_substitute_groups(conn, groups)
            _insert_items(conn, items)
            if aliases:
                _insert_ingredient_aliases(conn, aliases)

        items_sorted = sorted(items, key=lambda row: row["item_id"])

        total_truncations = 0
        total_stock_rows = 0
        total_shipment_rows = 0

        with conn:
            for item in items_sorted:
                stock_rows, shipment_rows, truncations = _simulate_item(item, seed, days)
                conn.executemany(
                    "INSERT INTO stock_usage_daily (item_id, date, usage_qty, incoming_qty,"
                    " closing_stock) VALUES (:item_id, :date, :usage_qty, :incoming_qty,"
                    " :closing_stock)",
                    stock_rows,
                )
                conn.executemany(
                    "INSERT INTO incoming_shipments (item_id, order_date, expected_date,"
                    " expected_qty, actual_date, actual_qty, status) VALUES (:item_id,"
                    " :order_date, :expected_date, :expected_qty, :actual_date, :actual_qty,"
                    " :status)",
                    shipment_rows,
                )
                total_truncations += truncations
                total_stock_rows += len(stock_rows)
                total_shipment_rows += len(shipment_rows)

        generated_at = base_date_obj.isoformat() + "T09:30:00"
        meta_rows = [
            ("seed", str(seed)),
            ("base_date", base_date_obj.isoformat()),
            ("item_count", str(len(items))),
            ("generated_at", generated_at),
            ("data_version", "1"),
            ("config_hash", "baseline-only"),
        ]
        with conn:
            conn.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", meta_rows)

        content_hash = compute_content_hash(conn)
        with conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('content_hash', ?)", (content_hash,)
            )
    finally:
        conn.close()

    elapsed = time.monotonic() - start

    return GenerationSummary(
        item_count=len(items),
        timeseries_row_count=total_stock_rows,
        shipment_count=total_shipment_rows,
        truncation_count=total_truncations,
        content_hash=content_hash,
        elapsed_seconds=elapsed,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 결정적 데이터셋 생성기(베이스라인 전용 경로)"
    )
    parser.add_argument("--out", required=True, help="출력 SQLite 파일 경로")
    parser.add_argument("--seed", required=True, type=int, help="결정적 생성 시드")
    parser.add_argument("--base-date", required=True, help="기준일(YYYY-MM-DD)")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="정상 운영 패턴만 생성(현재 유일하게 구현된 경로)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="시나리오 config 경로(받기만 하고 --baseline-only에서는 무시함)",
    )
    return parser


def main(argv: list[str] | None = None) -> GenerationSummary:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if not args.baseline_only:
        parser.error("시나리오 주입은 미구현(S-12)")

    summary = generate_baseline(args.out, seed=args.seed, base_date=args.base_date)

    print(f"품목 수: {summary.item_count}")
    print(f"시계열 행 수: {summary.timeseries_row_count}")
    print(f"발주 건수: {summary.shipment_count}")
    print(f"절삭 카운터: {summary.truncation_count}")
    print(f"content_hash: {summary.content_hash}")
    print(f"실행 시간: {summary.elapsed_seconds:.2f}초")

    return summary


if __name__ == "__main__":
    main()
