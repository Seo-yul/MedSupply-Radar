"""통합조회 서비스 계층 — 화면이 소비할 실데이터 조회·파생을 캐시와 함께 제공한다.

queries.py(순수 SQL)를 조합해 화면 친화적 DataFrame을 만드는 계층이다. 새 SQL이
필요하면 이 계층이 아니라 medsupply/data/queries.py에 추가한다(계층 규칙,
task-M15-brief.md).

계층 규칙 명문화(2주차 브랜치 리뷰 F6 룰링, task-M23-brief.md): services는
st.cache_data가 필요한 목록·상세 집계 조회의 캐시 계층이다. 단건 메타·건수·부가
조회는 뷰(medsupply/views/*.py)가 queries를 직접 호출해도 된다(전부 읽기 — 이
계층을 거치지 않아도 계층 위반이 아니다). 쓰기는 언제나 medsupply/data/writer를
거친다(단일 쓰기 경로 원칙, 화면·서비스 어디서도 그 앞을 가로막지 않는다). 기존
뷰들의 queries 직접 호출 지점은 이 규칙이 이미 허용하는 범위라 리팩터 대상이
아니다.
"""

from __future__ import annotations
