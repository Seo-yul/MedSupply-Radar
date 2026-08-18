"""MedSupply Radar 전역 설정.

격리 원칙(docs/data-model.md §4): 시나리오·ground truth 경로는 여기에 두지 않는다.
medsupply/ 전체와 app.py는 data/scenarios/ 이하 경로를 어떤 형태로도 참조하지 않는다.
"""

from __future__ import annotations

from pathlib import Path

DB_PATH = Path("data/medsupply.db")
LLM_CACHE_PATH = Path("data/llm_cache.db")

# 판정 기준 시각의 잠정 문자열 상수. 이후 태스크가 meta 테이블(base_date/generated_at) 조회로
# 대체할 수 있도록 자리를 잡아둔다 — 지금은 단일 상수로만 존재한다.
AS_OF = "2026-08-01T09:30:00"
