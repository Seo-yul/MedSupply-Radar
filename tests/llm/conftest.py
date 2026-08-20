"""tests/llm/ 전용 공용 픽스처.

LLM_CACHE_PATH를 매 테스트마다 격리된 tmp_path로 돌려, 캐시 파일(data/llm_cache.db)이
테스트 실행으로 생성·오염되지 않게 하고 테스트 간 상태 누수를 막는다.

이 격리가 필요한 이유: M-12에서 complete_json이 cache_key를 실제로 사용하게 되면서,
test_client_failover.py::test_anthropic_success_path처럼 cache_key를 명시하는 기존
테스트가 (수정 없이도) 캐시 계층과 실제로 상호작용하게 된다. 그 테스트는 고정 문자열
cache_key="unused-in-v1"을 쓰므로, settings.LLM_CACHE_PATH를 격리하지 않으면 실제
data/llm_cache.db에 항목이 남아 다음 실행에서 캐시 히트로 바뀌어(cache_hit=False를
기대하는 기존 assert가 깨짐) 테스트가 실행 순서/횟수에 따라 흔들리게 된다. complete_json은
force_refresh를 제외하면 경로 인자를 받지 않으므로, settings.LLM_CACHE_PATH를
모듈 속성 단위로 monkeypatch하는 것이 기존 테스트 파일을 건드리지 않고 격리하는 유일한
방법이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from medsupply import settings


@pytest.fixture(autouse=True)
def _isolated_llm_cache_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(settings, "LLM_CACHE_PATH", tmp_path / "llm_cache.db")
