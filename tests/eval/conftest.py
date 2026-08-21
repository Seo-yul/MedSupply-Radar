"""tests/eval/ 전용 공용 픽스처.

LLM_CACHE_PATH를 매 테스트마다 격리된 tmp_path로 돌려, 캐시 파일(data/llm_cache.db)이
테스트 실행으로 생성·오염되지 않게 하고 테스트 간 상태 누수를 막는다(tests/llm/conftest.py
와 동일한 격리 기법 — Task X-1 후속 조치).

이 격리가 필요한 이유: test_rubric_fixtures.py::test_smoke_real_judge_matches_expected_range
는 ANTHROPIC_API_KEY/OPENAI_API_KEY가 있는 환경에서 cache_key를 넘겨 실제 judge 호출을
수행한다. complete_json은 force_refresh를 제외하면 경로 인자를 받지 않고 호출마다
settings.LLM_CACHE_PATH를 새로 읽어 cache_get/cache_put에 넘기므로, 이 값을 격리하지
않으면 그 실제 호출 결과가 실제 data/llm_cache.db에 그대로 쌓인다 — "LLM 키 미설정·캐시
DB 부재가 기본"을 전제하는 다른 여러 곳(예: 평가 페이지 LLM 사용량 카드, Task X-1)과
어긋나고, 실행 환경에 따라 표준 작업 트리를 오염시킨다(재현: 격리 전 `pytest tests/eval/`
1회만으로 data/llm_cache.db가 새로 생겼고, 이 환경은 실제로 ANTHROPIC_API_KEY가 설정돼
있어 스모크 테스트가 skip되지 않고 실 API 호출까지 수행됨을 확인했다).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medsupply import settings


@pytest.fixture(autouse=True)
def _isolated_llm_cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "LLM_CACHE_PATH", tmp_path / "llm_cache.db")
