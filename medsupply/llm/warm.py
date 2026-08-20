"""LLM 캐시 선워밍 서비스(Task M-27) — process_notice·explain_item 일괄 선계산.

시연·오프라인 모드를 위해 공고 추출(M-14 process_notice)·위험 원인 설명(M-21 explain_item)
생성물을 미리 캐시·DB에 채워 넣는다. 이 모듈 자체는 LLM을 직접 호출하지 않는다 — 이미
완성된 process_notice(medsupply.llm.mapping)·explain_item(medsupply.llm.explanation)에
그대로 위임하고, warm_cache()는 "무엇을 어떤 순서로 호출할지"와 "건별 실패를 어떻게
격리·집계할지"만 책임진다.

scope='all'(기본)은 공고(process_notice)를 먼저 전부 처리한 뒤 설명(explain_item)을
처리한다 — 순서가 고정인 이유: explain_item → collect_risk_evidence
(medsupply.llm.grounding)가 근거에 포함하는 활성 공고 목록(active_notices)은
notice_item_map(공고→품목 매핑, process_notice가 영속화)을 통해서만 채워진다. 공고 처리를
뒤로 미루면 같은 실행 안에서도 아직 매핑되지 않은 공고가 있는 채로 설명이 생성돼(활성
공고 누락 → phantom_notice 오탐 등 근거 품질 저하) 시연 데이터의 정합성이 흔들린다.

설명 대상 품목은 "최신 run(medsupply.data.queries.get_latest_runs)에서 grade가
위험·경고·주의인 품목"만이다 — 정상 등급은 애초에 시연에서 노출되지 않는 화면 요소이므로
캐시를 채울 이유가 없다(브리프: "시연 노출 대상만"). item_id 오름차순으로 결정적으로
정렬한다. 최신 run이 아예 없으면(risk_results 빈 DB) 대상 없음 — 에러가 아니다.

건별 실패는 서로 격리된다: 한 공고/품목의 process_notice·explain_item 호출이 예외를
던져도(LLM 키 미설정·호출 실패 등) 그 건만 실패로 기록하고 나머지 대상 처리를 계속한다
(scripts/process_notices.py의 건별 격리 원칙과 동일). cache_hit 집계는 성공한 결과의
cache_hit 불리언만 합산한다(실패 건은 결과 자체가 없으므로 집계에서 자연히 제외된다).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from medsupply.data import queries
from medsupply.llm.explanation import explain_item
from medsupply.llm.mapping import process_notice

#: warm_cache(scope=...)에 허용되는 값(scripts/warm_cache.py의 --scope choices와 동일).
_VALID_SCOPES = ("all", "notices", "explanations")

#: 설명 대상 등급 — 정상 제외(시연 노출 대상만, 브리프 고정값).
_EXPLANATION_TARGET_GRADES = ("위험", "경고", "주의")


@dataclass(frozen=True)
class WarmReport:
    """warm_cache()의 반환값 — CLI 요약 출력·집계에 쓰인다.

    notices_failed·explanations_failed는 실패한 notice_id/item_id를 처리 시도 순서 그대로
    담은 튜플이다. scope가 해당하지 않는 쪽(예: scope='notices'일 때의 explanations_*)은
    total=ok=0, failed=()로 "미실행"을 표현한다 — 실행했는데 대상이 0건인 경우와 값으로는
    구분되지 않는다(둘 다 미수행 0건이라는 점에서 의미상 동일하다).
    """

    notices_total: int
    notices_ok: int
    notices_failed: tuple[str, ...]
    explanations_total: int
    explanations_ok: int
    explanations_failed: tuple[str, ...]
    cache_hits: int


def _list_notice_ids(conn: sqlite3.Connection) -> list[str]:
    """전 공고 notice_id 목록(medsupply.data.queries.get_notices, 결정적 순서)."""
    return list(queries.get_notices(conn)["notice_id"])


def _list_explanation_target_item_ids(conn: sqlite3.Connection) -> list[str]:
    """설명 대상 품목 = 최신 run에서 grade ∈ {위험,경고,주의}, item_id 오름차순(결정적).

    최신 run이 아예 없으면(risk_results 빈 DB) 대상 없음 — 빈 리스트(에러 아님).
    """
    latest_runs = queries.get_latest_runs(conn, 1)
    if not latest_runs:
        return []

    results = queries.get_risk_results(conn, latest_runs[0])
    targets = results.loc[results["grade"].isin(_EXPLANATION_TARGET_GRADES), "item_id"]
    return sorted(targets)


def _warm_notices(
    conn: sqlite3.Connection,
    *,
    force_refresh: bool,
    progress: Callable[[str], None] | None,
) -> tuple[int, int, tuple[str, ...], int]:
    """전 공고를 process_notice에 위임한다. Returns: (total, ok, failed_ids, cache_hits)."""
    ok = 0
    failed: list[str] = []
    cache_hits = 0

    notice_ids = _list_notice_ids(conn)
    for notice_id in notice_ids:
        try:
            result = process_notice(conn, notice_id, force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001 - 건별 실패를 격리하고 계속 진행하는 게 목적
            failed.append(notice_id)
            if progress is not None:
                progress(f"[notices] {notice_id}: 실패 - {exc}")
            continue

        ok += 1
        if result.cache_hit:
            cache_hits += 1
        if progress is not None:
            progress(f"[notices] {notice_id}: 완료 (cache_hit={result.cache_hit})")

    return len(notice_ids), ok, tuple(failed), cache_hits


def _warm_explanations(
    conn: sqlite3.Connection,
    *,
    force_refresh: bool,
    progress: Callable[[str], None] | None,
) -> tuple[int, int, tuple[str, ...], int]:
    """설명 대상 품목(item_id 오름차순)을 explain_item에 위임한다.

    Returns: (total, ok, failed_ids, cache_hits).
    """
    ok = 0
    failed: list[str] = []
    cache_hits = 0

    item_ids = _list_explanation_target_item_ids(conn)
    for item_id in item_ids:
        try:
            result = explain_item(conn, item_id, force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001 - 건별 실패를 격리하고 계속 진행하는 게 목적
            failed.append(item_id)
            if progress is not None:
                progress(f"[explanations] {item_id}: 실패 - {exc}")
            continue

        ok += 1
        if result.cache_hit:
            cache_hits += 1
        if progress is not None:
            progress(f"[explanations] {item_id}: 완료 (cache_hit={result.cache_hit})")

    return len(item_ids), ok, tuple(failed), cache_hits


def warm_cache(
    conn: sqlite3.Connection,
    *,
    scope: str = "all",
    force_refresh: bool = False,
    progress: Callable[[str], None] | None = None,
) -> WarmReport:
    """공고·설명 LLM 생성물을 일괄 선계산해 캐시·DB를 채운다(Task M-27).

    Args:
        conn: medsupply.data 계층 커넥션(sqlite3.Row row_factory 가정).
        scope: 'all'(기본, 공고 먼저 → 설명) | 'notices'(공고만) | 'explanations'(설명만).
        force_refresh: True면 LLM 캐시를 무시하고 항상 재호출한다(process_notice·
            explain_item에 그대로 전파).
        progress: 처리 건마다(성공·실패 모두) 호출되는 콜백(CLI 출력용). None이면 무시.

    Returns:
        WarmReport.

    Raises:
        ValueError: scope가 'all'|'notices'|'explanations' 중 하나가 아니면.
    """
    if scope not in _VALID_SCOPES:
        raise ValueError(f"unknown scope: {scope!r} (expected one of {_VALID_SCOPES})")

    notices_total = notices_ok = 0
    notices_failed: tuple[str, ...] = ()
    explanations_total = explanations_ok = 0
    explanations_failed: tuple[str, ...] = ()
    cache_hits = 0

    if scope in ("all", "notices"):
        notices_total, notices_ok, notices_failed, notice_cache_hits = _warm_notices(
            conn, force_refresh=force_refresh, progress=progress
        )
        cache_hits += notice_cache_hits

    if scope in ("all", "explanations"):
        explanations_total, explanations_ok, explanations_failed, expl_cache_hits = (
            _warm_explanations(conn, force_refresh=force_refresh, progress=progress)
        )
        cache_hits += expl_cache_hits

    return WarmReport(
        notices_total=notices_total,
        notices_ok=notices_ok,
        notices_failed=notices_failed,
        explanations_total=explanations_total,
        explanations_ok=explanations_ok,
        explanations_failed=explanations_failed,
        cache_hits=cache_hits,
    )
