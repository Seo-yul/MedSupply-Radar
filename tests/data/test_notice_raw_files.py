"""data/notices/*, data/reference/ingredient_aliases.csv 수집물 정합성 검증.

M-09가 공개 출처에서 수집한 **실제** 공급중단·부족 공고 원문(data/notices/raw/*.txt)과
그 색인(data/notices/notices_index.csv), 수집 과정에서 만든 성분 별칭 초안
(data/reference/ingredient_aliases.csv)이 기획서 계약을 어기지 않음을 고정한다.

이 테스트는 공고 "내용"의 사실성을 검증하지 않는다(자동화 불가능). 대신
- 산출물 형식(헤더 3줄, 색인 스키마, 날짜 포맷, 허용값)
- 파일 존재·색인 정합(orphan 없음)
- notices_index.csv가 시나리오 연계 컬럼을 갖지 않는다는 격리 원칙
- ingredient_aliases.csv의 ingredient_code가 실제 data/reference/ingredients.csv와
  정합한다는 것
을 강제해, 다음 태스크(S-02 별칭 검수, 추출 파이프라인)가 기대하는 최소 계약을 지킨다.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NOTICES_DIR = REPO_ROOT / "data" / "notices"
RAW_DIR = NOTICES_DIR / "raw"
INDEX_CSV = NOTICES_DIR / "notices_index.csv"
REFERENCE_DIR = REPO_ROOT / "data" / "reference"
INGREDIENTS_CSV = REFERENCE_DIR / "ingredients.csv"
ALIASES_CSV = REFERENCE_DIR / "ingredient_aliases.csv"

# schema.sql의 notices.notice_type CHECK 제약과 동일(medsupply/data/schema.sql).
ALLOWED_NOTICE_TYPES = {"공급중단", "공급부족", "정상화", "기타"}
ALLOWED_ALIAS_TYPES = {"kr", "en", "salt", "abbr"}

INDEX_HEADER = ["file", "published_date", "title", "source", "source_url", "notice_type"]
ALIASES_HEADER = ["alias", "ingredient_code", "alias_type"]

# notices_index.csv는 공고 원문 색인일 뿐, 시나리오·품목과 직접 연결되지 않는다(격리 원칙,
# task-M09-brief.md). 아래 이름 중 하나라도 헤더에 나타나면 원칙 위반으로 간주한다.
FORBIDDEN_INDEX_COLUMNS = {
    "scenario_id",
    "scenario",
    "item_id",
    "item_code",
    "ingredient_code",
    "standard_code",
    "substitute_group_id",
}

COLLECTED_AT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$"
)
INGREDIENT_CODE_PATTERN = re.compile(r"^ING-\d{3}$")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as fp:
        return list(csv.DictReader(fp))


def _header(path: Path) -> list[str]:
    with path.open(encoding="utf-8", newline="") as fp:
        return next(csv.reader(fp))


@pytest.fixture(scope="module")
def index_rows() -> list[dict[str, str]]:
    return _read_csv(INDEX_CSV)


@pytest.fixture(scope="module")
def ingredient_codes() -> set[str]:
    return {row["ingredient_code"] for row in _read_csv(INGREDIENTS_CSV)}


@pytest.fixture(scope="module")
def alias_rows() -> list[dict[str, str]]:
    return _read_csv(ALIASES_CSV)


@pytest.fixture(scope="module")
def raw_txt_files() -> list[Path]:
    return sorted(RAW_DIR.glob("*.txt"))


# ---------------------------------------------------------------------------
# 파일 존재
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [NOTICES_DIR, RAW_DIR, INDEX_CSV, ALIASES_CSV])
def test_paths_exist(path: Path) -> None:
    assert path.exists(), f"{path} 가 없다"


def test_at_least_one_notice_collected(raw_txt_files: list[Path]) -> None:
    """0건이면 BLOCKED — 브리프의 최소 성공 조건."""
    assert len(raw_txt_files) >= 1, "수집된 공고 원문이 0건이다"


def test_at_most_twenty_notices(raw_txt_files: list[Path]) -> None:
    """기획서 목표는 1·2차 누적 최대 20건(M-09 8건 + M-10 12건)."""
    assert len(raw_txt_files) <= 20


# ---------------------------------------------------------------------------
# notices_index.csv 스키마 · 격리 원칙
# ---------------------------------------------------------------------------


def test_index_header_matches_spec() -> None:
    assert _header(INDEX_CSV) == INDEX_HEADER


def test_index_has_no_scenario_linkage_columns() -> None:
    """격리 원칙: notices_index.csv는 시나리오·품목 연계 컬럼을 갖지 않는다."""
    header = set(_header(INDEX_CSV))
    leaked = header & FORBIDDEN_INDEX_COLUMNS
    assert not leaked, f"금지된 시나리오 연계 컬럼이 섞여 있다: {leaked}"


def test_index_row_count_matches_raw_files(index_rows, raw_txt_files) -> None:
    assert len(index_rows) == len(raw_txt_files)


def test_index_no_duplicate_file_entries(index_rows) -> None:
    files = [row["file"] for row in index_rows]
    duplicates = [v for v, n in Counter(files).items() if n > 1]
    assert not duplicates, f"notices_index.csv에 중복된 file 값: {duplicates}"


def test_index_files_all_exist_on_disk(index_rows) -> None:
    missing = [row["file"] for row in index_rows if not (RAW_DIR / row["file"]).is_file()]
    assert not missing, f"색인에는 있지만 실존하지 않는 파일: {missing}"


def test_every_raw_file_is_indexed(index_rows, raw_txt_files) -> None:
    """orphan 방지: raw/*.txt 는 전부 색인에 있어야 한다(반대 방향은 위 테스트가 담당)."""
    indexed = {row["file"] for row in index_rows}
    on_disk = {p.name for p in raw_txt_files}
    orphans = on_disk - indexed
    assert not orphans, f"색인에 없는 raw 파일: {orphans}"


def test_index_rows_have_no_blank_required_fields(index_rows) -> None:
    for row in index_rows:
        for col in INDEX_HEADER:
            assert row[col].strip(), f"{row.get('file')}: {col} 값이 비어있다"


def test_index_published_date_is_iso(index_rows) -> None:
    for row in index_rows:
        value = row["published_date"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", value), f"{row['file']}: ISO 날짜 형식 아님({value})"
        date.fromisoformat(value)  # 실재하는 달력 날짜인지까지 검증


def test_index_notice_type_allowed(index_rows) -> None:
    for row in index_rows:
        assert row["notice_type"] in ALLOWED_NOTICE_TYPES, (
            f"{row['file']}: 허용되지 않는 notice_type({row['notice_type']!r})"
        )


def test_index_source_url_looks_like_url(index_rows) -> None:
    for row in index_rows:
        assert row["source_url"].startswith("http"), (
            f"{row['file']}: source_url이 URL 형태가 아니다({row['source_url']!r})"
        )


# ---------------------------------------------------------------------------
# raw/*.txt 파일 형식 — 헤더 3줄 + 원문 본문
# ---------------------------------------------------------------------------


def _read_header_lines(path: Path) -> list[str]:
    with path.open(encoding="utf-8") as fp:
        return [next(fp).rstrip("\n") for _ in range(3)]


def test_raw_file_first_three_lines_are_required_headers(raw_txt_files) -> None:
    for path in raw_txt_files:
        source_line, collected_line, type_line = _read_header_lines(path)
        assert source_line.startswith("# source: "), f"{path.name}: 1행이 '# source: '로 시작하지 않음"
        assert source_line[len("# source: "):].strip().startswith("http"), (
            f"{path.name}: source 값이 URL이 아님"
        )
        assert collected_line.startswith("# collected_at: "), (
            f"{path.name}: 2행이 '# collected_at: '로 시작하지 않음"
        )
        collected_value = collected_line[len("# collected_at: "):].strip()
        assert COLLECTED_AT_PATTERN.match(collected_value), (
            f"{path.name}: collected_at이 ISO8601이 아님({collected_value!r})"
        )
        assert type_line.startswith("# notice_type: "), (
            f"{path.name}: 3행이 '# notice_type: '로 시작하지 않음"
        )
        type_value = type_line[len("# notice_type: "):].strip()
        assert type_value in ALLOWED_NOTICE_TYPES, (
            f"{path.name}: notice_type이 허용값이 아님({type_value!r})"
        )


def test_raw_file_has_body_after_headers(raw_txt_files) -> None:
    """3줄 헤더 아래에 실제 원문 본문이 있어야 한다(빈 파일 금지)."""
    for path in raw_txt_files:
        lines = path.read_text(encoding="utf-8").splitlines()
        body = "\n".join(lines[3:]).strip()
        assert body, f"{path.name}: 헤더 3줄 뒤에 본문이 없다"


def test_raw_file_notice_type_matches_index(index_rows) -> None:
    """파일 헤더 3행의 notice_type과 색인의 notice_type이 서로 어긋나면 안 된다."""
    by_file = {row["file"]: row["notice_type"] for row in index_rows}
    for filename, index_type in by_file.items():
        path = RAW_DIR / filename
        _, _, type_line = _read_header_lines(path)
        file_type = type_line[len("# notice_type: "):].strip()
        assert file_type == index_type, (
            f"{filename}: 파일 헤더 notice_type({file_type}) != 색인 notice_type({index_type})"
        )


def test_raw_file_names_follow_slug_pattern(raw_txt_files) -> None:
    """NNN_YYYY-MM-DD_슬러그.txt (NNN=001부터 시작, 한글 슬러그 허용)."""
    pattern = re.compile(r"^\d{3}_\d{4}-\d{2}-\d{2}_.+\.txt$")
    for path in raw_txt_files:
        assert pattern.match(path.name), f"{path.name}: 파일명이 NNN_날짜_슬러그.txt 형식이 아님"


# ---------------------------------------------------------------------------
# ingredient_aliases.csv
# ---------------------------------------------------------------------------


def test_aliases_header() -> None:
    assert _header(ALIASES_CSV) == ALIASES_HEADER


def test_aliases_row_count_at_least_30(alias_rows) -> None:
    assert len(alias_rows) >= 30, f"별칭이 30행 미만이다({len(alias_rows)}행)"


def test_aliases_rows_have_no_blank_values(alias_rows) -> None:
    for row in alias_rows:
        for col in ALIASES_HEADER:
            assert row[col].strip(), f"{row}: {col} 값이 비어있다"


def test_aliases_type_allowed(alias_rows) -> None:
    for row in alias_rows:
        assert row["alias_type"] in ALLOWED_ALIAS_TYPES, (
            f"{row['alias']}: 허용되지 않는 alias_type({row['alias_type']!r})"
        )


def test_aliases_ingredient_code_format(alias_rows) -> None:
    for row in alias_rows:
        assert INGREDIENT_CODE_PATTERN.match(row["ingredient_code"]), (
            f"{row['alias']}: ingredient_code 형식 오류({row['ingredient_code']!r})"
        )


def test_aliases_ingredient_code_exists_in_ingredients_master(
    alias_rows, ingredient_codes
) -> None:
    """별칭 CSV의 ingredient_code는 반드시 data/reference/ingredients.csv에 존재해야 한다."""
    unknown = sorted(
        {row["ingredient_code"] for row in alias_rows} - ingredient_codes
    )
    assert not unknown, f"ingredients.csv에 없는 ingredient_code: {unknown}"


def test_aliases_no_duplicate_alias(alias_rows) -> None:
    values = [row["alias"] for row in alias_rows]
    duplicates = [v for v, n in Counter(values).items() if n > 1]
    assert not duplicates, f"alias 중복: {duplicates}"


def test_aliases_not_identical_to_canonical_ingredient_names(
    alias_rows, ingredient_codes
) -> None:
    """별칭은 ingredients.csv 정본 표기와 달라야 값어치가 있다(중복 정보 방지)."""
    canonical = {row["ingredient_code"]: row for row in _read_csv(INGREDIENTS_CSV)}
    for row in alias_rows:
        master = canonical[row["ingredient_code"]]
        assert row["alias"] != master["ingredient_name_kr"], (
            f"{row['alias']}: ingredients.csv의 ingredient_name_kr과 동일(별칭 아님)"
        )
        assert row["alias"] != master["ingredient_name_en"], (
            f"{row['alias']}: ingredients.csv의 ingredient_name_en과 동일(별칭 아님)"
        )
