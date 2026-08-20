"""추출 결과 → 기관 품목·대체군 결정적 매핑 + 추출→매핑→영속화 파이프라인(M-14).

map_extraction_to_items()는 NoticeExtraction(M-13 LLM 추출 결과)을 기관이 실제로
보유한 items·ingredients에 연결한다. 이 조인에는 LLM이 전혀 관여하지 않는다 — 정규화한
문자열을 별칭 사전(ingredient_aliases)·품목명과 결정적으로 대조할 뿐이다(난수 없음,
동일 입력은 항상 동일 출력. TestDeterminism이 이를 고정한다).

process_notice()는 notices.raw_text 로드 → extract_notice(M-13, LLM 호출) →
map_extraction_to_items(이 모듈, LLM 미관여) → writer.save_notice_extraction(영속화)
을 한 번에 수행하는 공고 1건 처리 단위다. scripts/process_notices.py가 이 함수를
호출해 전 공고를 일괄 처리한다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from medsupply.data import writer
from medsupply.llm.extraction import extract_notice
from medsupply.llm.schemas import NoticeExtraction

#: 부분 포함 매칭(규칙 2c)에서 유효한 것으로 인정하는 최소 문자열 길이. 이보다 짧은
#: 문자열의 포함 관계는 오매핑 위험이 커서 무시한다(브리프 고정값).
_PARTIAL_MATCH_MIN_LEN = 4


@dataclass(frozen=True)
class MappingResult:
    """map_extraction_to_items()의 반환값.

    matched_ingredient_codes: 성분명·별칭 매칭(정확·부분 통틀어)으로 식별된
        ingredient_code 오름차순 튜플. 제품명 매칭 경로로만 찾은 경우는 포함하지 않는다.
    mapped: {item_id, substitute_group_id, match_basis, needs_review} 딕셔너리의
        item_id 오름차순 튜플. match_basis는 'ingredient'|'ingredient_partial'|'product'
        중 하나다.
    unmatched_ingredients: 정확·부분 매칭 어느 쪽에도 걸리지 않은
        extraction.ingredient_names 원소.
    unmatched_products: 제품명 매칭을 시도했는데(성분 매칭이 전부 실패해 보조 경로가
        실행된 경우에만 시도한다) items.item_name과 일치하지 않은
        extraction.product_names 원소.
    """

    matched_ingredient_codes: tuple[str, ...]
    mapped: tuple[dict, ...]
    unmatched_ingredients: tuple[str, ...]
    unmatched_products: tuple[str, ...]


def _norm(s: str) -> str:
    """casefold + 모든 공백 제거(규칙 1). 대소문자·공백 표기 차이를 흡수한다."""
    return "".join(s.casefold().split())


def _load_ingredient_candidates(
    conn: sqlite3.Connection,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """성분 매칭 후보를 결정적 순서로 적재한다.

    Returns:
        exact: 정규화 문자열 → ingredient_code 사전(정확 일치용). ingredient_name_kr/en을
            ingredient_code 오름차순으로 먼저 적재한 뒤 ingredient_aliases를
            (ingredient_code, alias) 오름차순으로 적재한다 — 동일 정규화 문자열이 둘
            이상의 ingredient_code에 걸리는 이상 상황에서도(setdefault) 항상 같은
            승자가 결정되도록 적재 순서를 고정한다.
        candidates: (정규화 문자열, ingredient_code) 목록(부분 포함 매칭용). 정렬돼 있어
            반복·반환 순서가 결정적이다.
    """
    exact: dict[str, str] = {}
    candidates: list[tuple[str, str]] = []

    for row in conn.execute(
        "SELECT ingredient_code, ingredient_name_kr, ingredient_name_en"
        " FROM ingredients ORDER BY ingredient_code"
    ):
        code = row["ingredient_code"]
        for name in (row["ingredient_name_kr"], row["ingredient_name_en"]):
            if not name:
                continue
            normalized = _norm(name)
            exact.setdefault(normalized, code)
            candidates.append((normalized, code))

    for row in conn.execute(
        "SELECT alias, ingredient_code FROM ingredient_aliases ORDER BY ingredient_code, alias"
    ):
        normalized = _norm(row["alias"])
        if not normalized:
            continue
        exact.setdefault(normalized, row["ingredient_code"])
        candidates.append((normalized, row["ingredient_code"]))

    candidates.sort()
    return exact, candidates


def _partial_match_codes(normalized_name: str, candidates: list[tuple[str, str]]) -> list[str]:
    """규칙 2c: 별칭/성분명이 추출값에 포함되거나 그 역인 후보를 전부 찾는다(길이 ≥4만).

    candidates가 정규화 문자열 오름차순으로 정렬돼 있으므로 반환 순서도 결정적이다.
    동일 ingredient_code가 이름·별칭 둘 다에서 걸려도 한 번만 반환한다(중복 제거).
    """
    matched: list[str] = []
    seen: set[str] = set()
    for candidate, code in candidates:
        if code in seen:
            continue
        candidate_in_name = (
            len(candidate) >= _PARTIAL_MATCH_MIN_LEN and candidate in normalized_name
        )
        name_in_candidate = (
            len(normalized_name) >= _PARTIAL_MATCH_MIN_LEN and normalized_name in candidate
        )
        if candidate_in_name or name_in_candidate:
            matched.append(code)
            seen.add(code)
    return matched


def _match_ingredients(
    conn: sqlite3.Connection, ingredient_names: list[str]
) -> tuple[dict[str, str], list[str]]:
    """규칙 2 — extraction.ingredient_names 각각을 정확 매칭 우선, 실패 시 부분 매칭.

    Returns:
        (ingredient_code → basis('ingredient'|'ingredient_partial') 사전, 아무
        매칭도 없었던 원소 목록). 같은 ingredient_code가 어떤 이름에서는 정확 매칭,
        다른 이름에서는 부분 매칭으로 걸려도 'ingredient'(더 신뢰도 높은 근거)가
        우선한다 — 부분 매칭이 나중에 나와도 이미 확정된 'ingredient'를 부분 매칭으로
        내리지 않는다.
    """
    exact, candidates = _load_ingredient_candidates(conn)

    matches: dict[str, str] = {}
    unmatched: list[str] = []

    for name in ingredient_names:
        normalized = _norm(name)

        code = exact.get(normalized)
        if code is not None:
            matches[code] = "ingredient"
            continue

        partial_codes = _partial_match_codes(normalized, candidates)
        if partial_codes:
            for code in partial_codes:
                if matches.get(code) != "ingredient":
                    matches[code] = "ingredient_partial"
            continue

        unmatched.append(name)

    return matches, unmatched


def _items_for_ingredient_codes(conn: sqlite3.Connection, matches: dict[str, str]) -> list[dict]:
    """규칙 4 — 매칭된 ingredient_code들의 모든 items 행(item_id 오름차순)을 만든다."""
    codes = sorted(matches)
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        "SELECT item_id, ingredient_code, substitute_group_id FROM items"
        f" WHERE ingredient_code IN ({placeholders}) ORDER BY item_id",
        codes,
    ).fetchall()
    return [
        {
            "item_id": row["item_id"],
            "substitute_group_id": row["substitute_group_id"],
            "match_basis": matches[row["ingredient_code"]],
        }
        for row in rows
    ]


def _match_products(
    conn: sqlite3.Connection, product_names: list[str]
) -> tuple[list[dict], list[str]]:
    """규칙 3 — extraction.product_names를 items.item_name과 정규화 완전 일치로만 매칭.

    map_extraction_to_items에서 성분 매칭이 전부 실패했을 때만 호출되는 보조 경로다.
    부분 일치는 하지 않는다(오매핑 위험). item_id 오름차순으로 반환한다.
    """
    rows = conn.execute(
        "SELECT item_id, item_name, substitute_group_id FROM items ORDER BY item_id"
    ).fetchall()

    by_normalized_name: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_normalized_name.setdefault(_norm(row["item_name"]), []).append(row)

    mapped_by_item: dict[str, dict] = {}
    unmatched: list[str] = []

    for name in product_names:
        matched_rows = by_normalized_name.get(_norm(name))
        if not matched_rows:
            unmatched.append(name)
            continue
        for row in matched_rows:
            mapped_by_item[row["item_id"]] = {
                "item_id": row["item_id"],
                "substitute_group_id": row["substitute_group_id"],
                "match_basis": "product",
            }

    mapped = [mapped_by_item[item_id] for item_id in sorted(mapped_by_item)]
    return mapped, unmatched


def map_extraction_to_items(
    conn: sqlite3.Connection,
    extraction: NoticeExtraction,
    *,
    extraction_status: str = "자동확정",
) -> MappingResult:
    """추출 결과를 기관 품목에 결정적으로 연결한다(LLM 미관여, 브리프 매핑 규칙 그대로).

    순서: (1) ingredient_names를 성분명·별칭에 매칭(정확 우선, 부분 보조) → 매칭된
    ingredient_code의 모든 items 행을 mapped 후보로 삼는다(규칙 2·4). (2) 성분 매칭이
    전부 실패한 경우에만(매칭된 ingredient_code가 하나도 없을 때만) product_names를
    items.item_name 정확 일치로 매칭해 보조한다(규칙 3 — "2순위·성분 미매칭 시 보조").
    성분이 하나라도 매칭되면 product_names는 아예 시도하지 않는다. (3) needs_review는
    행 단위로 결정한다(규칙 5) — extraction_status가 '확인 필요'면 전 행 1, 아니면
    match_basis가 'ingredient_partial'인 행만 1이고 나머지('ingredient'·'product')는 0.

    아무것도 매칭되지 않으면(성분·제품명 둘 다 실패) mapped는 빈 튜플이고 두
    unmatched 필드에 실패한 원소가 남는다(규칙 6) — 이는 에러가 아니라 "기관이
    보유하지 않은 품목에 대한 공고"라는 정상적인 결과다.
    """
    ingredient_matches, unmatched_ingredients = _match_ingredients(conn, extraction.ingredient_names)

    if ingredient_matches:
        rows = _items_for_ingredient_codes(conn, ingredient_matches)
        unmatched_products: list[str] = []
    else:
        rows, unmatched_products = _match_products(conn, extraction.product_names)

    needs_review_all = extraction_status == "확인 필요"
    mapped = tuple(
        {
            "item_id": row["item_id"],
            "substitute_group_id": row["substitute_group_id"],
            "match_basis": row["match_basis"],
            "needs_review": 1
            if (needs_review_all or row["match_basis"] == "ingredient_partial")
            else 0,
        }
        for row in rows
    )

    return MappingResult(
        matched_ingredient_codes=tuple(sorted(ingredient_matches)),
        mapped=mapped,
        unmatched_ingredients=tuple(unmatched_ingredients),
        unmatched_products=tuple(unmatched_products),
    )


# ---------------------------------------------------------------------------
# process_notice — 추출(M-13) → 매핑(위) → 영속화(writer) 파이프라인
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoticeProcessingResult:
    """process_notice()의 반환값 — CLI 요약 출력·집계에 쓰인다."""

    notice_id: str
    status: str
    confidence: float
    mapped_count: int
    matched_ingredients: int
    cache_hit: bool


def process_notice(
    conn: sqlite3.Connection, notice_id: str, *, force_refresh: bool = False
) -> NoticeProcessingResult:
    """공고 1건을 추출 → 매핑 → 영속화까지 한 번에 수행한다.

    notices.raw_text를 로드하고(존재하지 않는 notice_id면 ValueError) extract_notice
    (M-13, LLM 호출·결정적 검증)로 구조화 추출 → map_extraction_to_items(이 모듈, LLM
    미관여)로 품목 매핑 → writer.save_notice_extraction으로 영속화한다. writer가
    notice_id PK로 INSERT OR REPLACE하고 notice_item_map을 통째로 교체하므로(writer
    기존 규칙), 같은 notice_id를 재처리해도 결과는 멱등하다.
    """
    row = conn.execute(
        "SELECT raw_text FROM notices WHERE notice_id = ?", (notice_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown notice_id: {notice_id!r}")
    raw_text = row["raw_text"]

    extraction_result = extract_notice(raw_text, notice_id=notice_id, force_refresh=force_refresh)

    mapping = map_extraction_to_items(
        conn, extraction_result.extraction, extraction_status=extraction_result.status
    )

    writer.save_notice_extraction(
        conn,
        notice_id=notice_id,
        payload=extraction_result.extraction.model_dump(),
        confidence=extraction_result.confidence,
        status=extraction_result.status,
        prompt_version=extraction_result.prompt_version,
        provider=extraction_result.provider,
        model=extraction_result.model,
        mapped=list(mapping.mapped),
    )

    return NoticeProcessingResult(
        notice_id=notice_id,
        status=extraction_result.status,
        confidence=extraction_result.confidence,
        mapped_count=len(mapping.mapped),
        matched_ingredients=len(mapping.matched_ingredient_codes),
        cache_hit=extraction_result.cache_hit,
    )
