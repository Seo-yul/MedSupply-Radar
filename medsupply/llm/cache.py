"""LLM 호출 결과 캐시(SQLite) — 캐시 키 규약과 조회·저장·워밍 통계.

메인 DB(medsupply.db)와 분리된 별도 파일(기본 data/llm_cache.db)에 저장한다(잠금 분리) —
마스터 플랜 결정 39. 캐시 키는 task+prompt_version+model+schema_version(=schema.__name__)+
sha256(정렬된 canonical payload)로 구성하며, run_id/generated_at/trace_id 같은 휘발 필드는
어느 깊이에서든 제거한 뒤 해시한다 — 같은 논리적 입력이 실행마다 달라지는 run_id 등
때문에 캐시 미스가 나지 않도록 하기 위함이다.

complete_json과의 통합(offline에서 캐시 히트 우선 포함)은 medsupply/llm/client.py가
수행한다. 이 모듈은 순수 캐시 계층(키 생성 + get/put/init/stats)만 담당하며, 순환 임포트를
피하기 위해 client.py를 모듈 최상단에서 임포트하지 않는다(LLMResult는 cache_get 안에서만
지연 임포트한다) — client.py가 이 모듈을 최상단에서 임포트하는 방향이다.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from medsupply import settings

if TYPE_CHECKING:
    from medsupply.llm.client import LLMResult

T = TypeVar("T", bound=BaseModel)

# build_cache_key가 해시 이전에 재귀적으로 제거하는 휘발 필드(마스터 플랜 결정 39).
_VOLATILE_KEYS = frozenset({"run_id", "generated_at", "trace_id"})

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS llm_cache (
    key TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    schema_name TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_used TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""


def _strip_volatile(value):
    """dict/list를 재귀적으로 순회하며 휘발 필드 키를 제거한 사본을 반환한다."""
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    return value


def build_cache_key(task: str, prompt_version: str, model: str, schema: type, payload: dict) -> str:
    """캐시 키를 결정적으로 생성한다.

    ``sha256(f"{task}|{prompt_version}|{model}|{schema.__name__}|{canonical}")``.
    ``canonical``은 payload에서 run_id/generated_at/trace_id를 어느 깊이에서든 제거한 뒤
    ``sort_keys=True``로 직렬화하므로, payload의 키 순서와 무관하게 동일한 값이면 항상
    같은 키가 나온다.
    """
    cleaned = _strip_volatile(payload)
    canonical = json.dumps(cleaned, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    raw = f"{task}|{prompt_version}|{model}|{schema.__name__}|{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def init_cache(path: str | Path = settings.LLM_CACHE_PATH) -> None:
    """llm_cache 테이블이 없으면 생성한다(멱등) — 모든 get/put이 내부에서 호출한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(_CREATE_TABLE_SQL)
        conn.commit()
    finally:
        conn.close()


def cache_get(key: str, schema: type[T], *, path: str | Path = settings.LLM_CACHE_PATH) -> LLMResult[T] | None:
    """캐시 히트 시 LLMResult(cache_hit=True)를, 미스면 None을 반환한다."""
    from medsupply.llm.client import LLMResult  # 지연 임포트(순환 임포트 회피)

    init_cache(path=path)
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT payload_json, provider, model_used, usage_json FROM llm_cache WHERE key = ?",
            (key,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None

    return LLMResult(
        data=schema.model_validate_json(row["payload_json"]),
        provider=row["provider"],
        model=row["model_used"],
        cache_hit=True,
        latency_ms=0,
        trace_id=None,
        usage=json.loads(row["usage_json"]),
    )


def cache_put(
    key: str,
    task: str,
    prompt_version: str,
    result: LLMResult,
    *,
    path: str | Path = settings.LLM_CACHE_PATH,
) -> None:
    """INSERT OR REPLACE로 캐시 항목을 기록한다(같은 key 재기록 시 덮어쓴다)."""
    init_cache(path=path)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO llm_cache"
            " (key, task, prompt_version, model, schema_name, payload_json,"
            "  provider, model_used, usage_json, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key,
                task,
                prompt_version,
                result.model,
                type(result.data).__name__,
                result.data.model_dump_json(),
                result.provider,
                result.model,
                json.dumps(result.usage, ensure_ascii=False),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def cache_stats(path: str | Path = settings.LLM_CACHE_PATH) -> dict:
    """{"entries": 전체 건수, "by_task": {task: 건수}} — 워밍 리포트용."""
    init_cache(path=path)
    conn = sqlite3.connect(path)
    try:
        entries = conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0]
        by_task = dict(conn.execute("SELECT task, COUNT(*) FROM llm_cache GROUP BY task").fetchall())
    finally:
        conn.close()
    return {"entries": entries, "by_task": by_task}
