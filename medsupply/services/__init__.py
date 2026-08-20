"""통합조회 서비스 계층 — 화면이 소비할 실데이터 조회·파생을 캐시와 함께 제공한다.

queries.py(순수 SQL)를 조합해 화면 친화적 DataFrame을 만드는 계층이다. 새 SQL이
필요하면 이 계층이 아니라 medsupply/data/queries.py에 추가한다(계층 규칙,
task-M15-brief.md).
"""

from __future__ import annotations
