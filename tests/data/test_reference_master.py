"""data/reference/*.csv 기준정보 마스터 정합성 검증.

이 테스트는 손으로 설계한 3개 기준정보 CSV(성분·대체군·품목)가
medsupply/data/schema.sql의 데이터 계약과 어긋나지 않음을 고정한다.

검증 축
- 스키마 대조: CSV 헤더 == schema.sql의 해당 테이블 컬럼(순서 포함)
- 규모: 성분 40+, 품목 100+, 대체군 30+
- PK 유일성 / 참조 정합(FK)
- 대체군 규칙: 품목의 (성분·함량·제형·투여경로)가 소속 대체군과 정확히 일치
- 대체군 밀도: 대체약 트리 시연이 성립하는 최소 밀도
- 코드 형식: 실제 WHO ATC 7자리 형식, 합성 표준코드 '99' 프리픽스 13자리
- 값 집합: 제형·투여경로 허용 집합, 필수의약품 플래그·비율
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REFERENCE_DIR = REPO_ROOT / "data" / "reference"
SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"

INGREDIENTS_CSV = REFERENCE_DIR / "ingredients.csv"
ITEMS_CSV = REFERENCE_DIR / "items_master.csv"
GROUPS_CSV = REFERENCE_DIR / "substitute_groups.csv"
SOURCES_MD = REFERENCE_DIR / "sources.md"

ATC_PATTERN = re.compile(r"^[A-Z]\d{2}[A-Z]{2}\d{2}$")
STANDARD_CODE_PATTERN = re.compile(r"^99\d{11}$")
INGREDIENT_CODE_PATTERN = re.compile(r"^ING-\d{3}$")
ITEM_ID_PATTERN = re.compile(r"^ITM-\d{4}$")
GROUP_ID_PATTERN = re.compile(r"^SG-\d{3}$")

# form/route에는 schema.sql의 CHECK 제약이 없다. 아래 집합은 본 프로젝트의 데이터 규약이며
# (data/reference/sources.md 참조) 그 강제는 이 테스트가 담당한다.
ALLOWED_FORMS = {"정제", "캡슐", "시럽", "주사", "바이알", "서방정", "산제", "패치"}
ALLOWED_ROUTES = {"경구", "정맥주사", "근육주사", "피하", "외용"}

BASE_SUPPLIERS = {"한빛제약", "대한제약", "유니메드", "메디팜", "그린바이오"}

# app.py 데모(medsupply/views/_demo.py)가 쓰던 6품목 — 시연 서사 연속성을 위해 이름을 보존한다.
DEMO_ITEM_NAMES = (
    "아세트아미노펜정 500mg",
    "세프트리악손주 1g",
    "아목시실린캡슐 500mg",
    "덱시부프로펜시럽",
    "메트포르민정 500mg",
    "아토르바스타틴정 10mg",
)

# 대체약 트리 시연의 기준 그룹(동일 조건 3품목)과 조건 불일치 후보 그룹.
ACETAMINOPHEN_TABLET_KEY = ("아세트아미노펜", "500mg", "정제", "경구")
ACETAMINOPHEN_ER_KEY = ("아세트아미노펜", "650mg", "서방정", "경구")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


def _header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as fp:
        return next(csv.reader(fp))


def _schema_columns(table: str) -> list[str]:
    """schema.sql에서 테이블의 컬럼명을 선언 순서대로 뽑는다(주석·제약절 제외)."""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"CREATE TABLE {table} \((.*?)\n\);", sql, flags=re.DOTALL
    )
    assert match is not None, f"schema.sql에 {table} 테이블 정의가 없다"
    columns: list[str] = []
    for raw_line in match.group(1).splitlines():
        line = raw_line.split("--")[0].strip()
        if not line:
            continue
        first = line.split()[0]
        if first.upper() in {"PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT"}:
            continue
        columns.append(first)
    return columns


@pytest.fixture(scope="module")
def ingredients() -> list[dict[str, str]]:
    return _read_csv(INGREDIENTS_CSV)


@pytest.fixture(scope="module")
def items() -> list[dict[str, str]]:
    return _read_csv(ITEMS_CSV)


@pytest.fixture(scope="module")
def groups() -> list[dict[str, str]]:
    return _read_csv(GROUPS_CSV)


# ---------------------------------------------------------------------------
# 파일 존재·스키마 대조
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", [INGREDIENTS_CSV, ITEMS_CSV, GROUPS_CSV, SOURCES_MD]
)
def test_reference_files_exist(path: Path) -> None:
    assert path.is_file(), f"{path} 가 없다"


@pytest.mark.parametrize(
    ("path", "table"),
    [
        (INGREDIENTS_CSV, "ingredients"),
        (GROUPS_CSV, "substitute_groups"),
        (ITEMS_CSV, "items"),
    ],
)
def test_csv_header_matches_schema_columns(path: Path, table: str) -> None:
    assert _header(path) == _schema_columns(table)


def test_no_commas_inside_values(ingredients, items, groups) -> None:
    """따옴표 이슈 회피 규칙 — 어떤 값에도 쉼표를 넣지 않는다."""
    for rows in (ingredients, items, groups):
        for row in rows:
            for key, value in row.items():
                assert "," not in value, f"{key}={value!r} 에 쉼표가 있다"


# ---------------------------------------------------------------------------
# 규모
# ---------------------------------------------------------------------------


def test_row_counts(ingredients, items, groups) -> None:
    assert len(ingredients) >= 40
    assert len(items) >= 100
    assert len(groups) >= 30


# ---------------------------------------------------------------------------
# PK 유일성
# ---------------------------------------------------------------------------


def test_primary_keys_are_unique(ingredients, items, groups) -> None:
    for rows, key in (
        (ingredients, "ingredient_code"),
        (items, "item_id"),
        (items, "standard_code"),
        (groups, "substitute_group_id"),
    ):
        values = [row[key] for row in rows]
        duplicates = [v for v, n in Counter(values).items() if n > 1]
        assert not duplicates, f"{key} 중복: {duplicates}"


def test_code_formats(ingredients, items, groups) -> None:
    for row in ingredients:
        assert INGREDIENT_CODE_PATTERN.match(row["ingredient_code"])
    for row in groups:
        assert GROUP_ID_PATTERN.match(row["substitute_group_id"])
    for row in items:
        assert ITEM_ID_PATTERN.match(row["item_id"])


def test_ingredient_codes_are_sequential(ingredients) -> None:
    expected = [f"ING-{i:03d}" for i in range(1, len(ingredients) + 1)]
    assert [row["ingredient_code"] for row in ingredients] == expected


def test_group_ids_are_sequential(groups) -> None:
    expected = [f"SG-{i:03d}" for i in range(1, len(groups) + 1)]
    assert [row["substitute_group_id"] for row in groups] == expected


def test_item_ids_are_sequential(items) -> None:
    expected = [f"ITM-{i:04d}" for i in range(1, len(items) + 1)]
    assert [row["item_id"] for row in items] == expected


# ---------------------------------------------------------------------------
# 참조 정합(FK)
# ---------------------------------------------------------------------------


def test_referential_integrity(ingredients, items, groups) -> None:
    ingredient_codes = {row["ingredient_code"] for row in ingredients}
    group_ids = {row["substitute_group_id"] for row in groups}

    for row in groups:
        assert row["ingredient_code"] in ingredient_codes, row

    for row in items:
        assert row["ingredient_code"] in ingredient_codes, row
        assert row["substitute_group_id"] in group_ids, row


def test_every_group_has_at_least_one_item(items, groups) -> None:
    used = {row["substitute_group_id"] for row in items}
    orphans = {row["substitute_group_id"] for row in groups} - used
    assert not orphans, f"품목이 없는 대체군: {sorted(orphans)}"


def test_every_ingredient_has_at_least_one_item(ingredients, items) -> None:
    used = {row["ingredient_code"] for row in items}
    orphans = {row["ingredient_code"] for row in ingredients} - used
    assert not orphans, f"품목이 없는 성분: {sorted(orphans)}"


# ---------------------------------------------------------------------------
# 대체군 규칙 — 동일 성분+함량+제형+투여경로 = 한 그룹
# ---------------------------------------------------------------------------


def test_item_attributes_match_its_group(items, groups) -> None:
    by_id = {row["substitute_group_id"]: row for row in groups}
    for item in items:
        group = by_id[item["substitute_group_id"]]
        actual = (
            item["ingredient_code"],
            item["strength"],
            item["form"],
            item["route"],
        )
        expected = (
            group["ingredient_code"],
            group["strength"],
            group["form"],
            group["route"],
        )
        assert actual == expected, f"{item['item_id']} 가 소속 대체군과 불일치"


def test_group_key_is_unique(groups) -> None:
    """(성분·함량·제형·투여경로)가 같은 그룹이 둘 이상 있으면 대체 탐색이 갈라진다."""
    keys = [
        (row["ingredient_code"], row["strength"], row["form"], row["route"])
        for row in groups
    ]
    duplicates = [k for k, n in Counter(keys).items() if n > 1]
    assert not duplicates, f"중복 대체군 키: {duplicates}"


def test_group_labels_are_present_and_unique(groups) -> None:
    labels = [row["group_label"] for row in groups]
    assert all(labels), "빈 group_label 이 있다"
    duplicates = [v for v, n in Counter(labels).items() if n > 1]
    assert not duplicates, f"group_label 중복: {duplicates}"


# ---------------------------------------------------------------------------
# 대체군 밀도 — 대체약 트리 시연의 핵심
# ---------------------------------------------------------------------------


def _group_sizes(items) -> Counter:
    return Counter(row["substitute_group_id"] for row in items)


def test_at_least_ten_groups_have_multiple_items(items) -> None:
    multi = [gid for gid, n in _group_sizes(items).items() if n >= 2]
    assert len(multi) >= 10, f"복수 품목 대체군이 {len(multi)}개뿐"


def test_multi_item_groups_have_distinct_suppliers(items) -> None:
    """같은 대체군 안에서는 공급사만 달라야 한다(공급사 중복 = 대체 후보가 아님)."""
    by_group: dict[str, list[str]] = {}
    for row in items:
        by_group.setdefault(row["substitute_group_id"], []).append(row["supplier"])
    for gid, suppliers in by_group.items():
        assert len(suppliers) == len(set(suppliers)), f"{gid} 공급사 중복: {suppliers}"


def _resolve_group_id(groups, ingredients, key) -> str:
    name_kr, strength, form, route = key
    code = next(
        row["ingredient_code"]
        for row in ingredients
        if row["ingredient_name_kr"] == name_kr
    )
    return next(
        row["substitute_group_id"]
        for row in groups
        if (row["ingredient_code"], row["strength"], row["form"], row["route"])
        == (code, strength, form, route)
    )


def test_acetaminophen_tablet_group_has_exactly_three_items(
    ingredients, items, groups
) -> None:
    gid = _resolve_group_id(groups, ingredients, ACETAMINOPHEN_TABLET_KEY)
    members = [row for row in items if row["substitute_group_id"] == gid]
    assert len(members) == 3
    assert {row["supplier"] for row in members} == {"한빛제약", "대한제약", "유니메드"}


def test_acetaminophen_extended_release_is_a_separate_single_item_group(
    ingredients, items, groups
) -> None:
    """650mg 서방정은 별도 그룹 1품목 — '조건 불일치' 후보 시연용."""
    tablet_gid = _resolve_group_id(groups, ingredients, ACETAMINOPHEN_TABLET_KEY)
    er_gid = _resolve_group_id(groups, ingredients, ACETAMINOPHEN_ER_KEY)
    assert er_gid != tablet_gid
    members = [row for row in items if row["substitute_group_id"] == er_gid]
    assert len(members) == 1


# ---------------------------------------------------------------------------
# 코드 형식 — ATC / 표준코드
# ---------------------------------------------------------------------------


def test_ingredient_atc_codes_are_well_formed(ingredients) -> None:
    for row in ingredients:
        assert ATC_PATTERN.match(row["atc_code"]), row


def test_item_atc_codes_are_well_formed(items) -> None:
    for row in items:
        assert ATC_PATTERN.match(row["atc_code"]), row


def test_item_atc_code_equals_its_ingredient_atc_code(ingredients, items) -> None:
    atc_by_code = {row["ingredient_code"]: row["atc_code"] for row in ingredients}
    for row in items:
        assert row["atc_code"] == atc_by_code[row["ingredient_code"]], row


def test_ingredient_atc_codes_are_unique(ingredients) -> None:
    """서로 다른 성분이 같은 ATC 5단계 코드를 쓰면 분류가 틀린 것이다."""
    codes = [row["atc_code"] for row in ingredients]
    duplicates = [v for v, n in Counter(codes).items() if n > 1]
    assert not duplicates, f"ATC 코드 중복: {duplicates}"


def test_standard_codes_are_synthetic_13_digits(items) -> None:
    for row in items:
        assert STANDARD_CODE_PATTERN.match(row["standard_code"]), row


# ---------------------------------------------------------------------------
# 값 집합 · 분포
# ---------------------------------------------------------------------------


def test_forms_and_routes_are_in_allowed_sets(items, groups) -> None:
    for rows in (items, groups):
        for row in rows:
            assert row["form"] in ALLOWED_FORMS, row
            assert row["route"] in ALLOWED_ROUTES, row


def test_pack_size_is_a_positive_integer(items) -> None:
    for row in items:
        assert row["pack_size"].isdigit() and int(row["pack_size"]) > 0, row


def test_is_essential_is_binary(items) -> None:
    for row in items:
        assert row["is_essential"] in {"0", "1"}, row


def test_essential_ratio_is_around_thirty_percent(items) -> None:
    ratio = sum(int(row["is_essential"]) for row in items) / len(items)
    assert 0.20 <= ratio <= 0.40, f"필수의약품 비율 {ratio:.2%}"


def test_suppliers_cover_existing_five_plus_synthetic_additions(items) -> None:
    suppliers = {row["supplier"] for row in items}
    assert BASE_SUPPLIERS <= suppliers, f"기존 공급사 누락: {BASE_SUPPLIERS - suppliers}"
    added = suppliers - BASE_SUPPLIERS
    assert 3 <= len(added) <= 5, f"합성 공급사는 3~5사여야 한다(현재 {len(added)}사: {sorted(added)})"


def test_ingredient_names_are_present_and_unique(ingredients) -> None:
    for key in ("ingredient_name_kr", "ingredient_name_en"):
        values = [row[key] for row in ingredients]
        assert all(values), f"빈 {key} 가 있다"
        duplicates = [v for v, n in Counter(values).items() if n > 1]
        assert not duplicates, f"{key} 중복: {duplicates}"


def test_item_names_are_unique(items) -> None:
    names = [row["item_name"] for row in items]
    duplicates = [v for v, n in Counter(names).items() if n > 1]
    assert not duplicates, f"item_name 중복: {duplicates}"


# ---------------------------------------------------------------------------
# 기존 데모 서사 연속성
# ---------------------------------------------------------------------------


def test_demo_item_names_are_preserved(items) -> None:
    names = {row["item_name"] for row in items}
    missing = set(DEMO_ITEM_NAMES) - names
    assert not missing, f"데모 품목명 누락: {sorted(missing)}"


@pytest.mark.parametrize(
    ("item_name", "supplier"),
    [
        ("아세트아미노펜정 500mg", "한빛제약"),
        ("세프트리악손주 1g", "메디팜"),
        ("아목시실린캡슐 500mg", "그린바이오"),
        ("덱시부프로펜시럽", "한빛제약"),
        ("메트포르민정 500mg", "유니메드"),
        ("아토르바스타틴정 10mg", "메디팜"),
    ],
)
def test_demo_item_supplier_mapping_is_preserved(items, item_name, supplier) -> None:
    row = next(r for r in items if r["item_name"] == item_name)
    assert row["supplier"] == supplier


def test_demo_ingredients_are_present(ingredients) -> None:
    required_en = {
        "Acetaminophen",
        "Ceftriaxone",
        "Amoxicillin",
        "Dexibuprofen",
        "Metformin",
        "Atorvastatin",
    }
    names = {row["ingredient_name_en"] for row in ingredients}
    assert required_en <= names, f"데모 성분 누락: {sorted(required_en - names)}"


# ---------------------------------------------------------------------------
# sources.md — 참조 체계 기록
# ---------------------------------------------------------------------------


def test_sources_md_documents_reference_system() -> None:
    text = SOURCES_MD.read_text(encoding="utf-8")
    for marker in ("ATC", "99", "합성"):
        assert marker in text, f"sources.md 에 {marker} 설명이 없다"
    assert "## 품절 시나리오 참조 사례" in text, "S-03이 채울 절 자리가 없다"
