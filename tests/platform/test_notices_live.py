"""Task M-17: notices.py 실데이터 렌더 테스트.

AppTest.from_function(notices.render 래퍼)을 온디스크 소형 표준 스냅샷(subprocess로
--baseline-only 생성)에 대해 실행한다(tests/platform/test_situation_live.py와 동일 관례).
--baseline-only 스냅샷에는 공고가 없다(공고 원문 적재는 scripts/load_notices.py의 별도
단계라 --baseline-only 파이프라인에 포함되지 않는다) — 정상 경로 테스트는 브리프 지시대로
공고·추출·매핑 행을 raw SQL로 직접 주입한다("테스트는 픽스처 DB에 추출·매핑 행을 직접
INSERT해 정상 경로를 검증하면 된다").

st.cache_data/st.cache_resource는 프로세스 전역이라 각 테스트에서 반드시 clear()해야
이전 테스트의 커넥션·결과가 재사용되지 않는다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings
from medsupply.services import inventory
from medsupply.services import notices as notices_service

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"

SEED = 20260801
BASE_DATE = "2026-08-01"

_TEST_NOTICE_ID = "N-TEST-001"
_TEST_NOTICE_TITLE = "테스트 품목 공급중단 안내"


def _run_notices() -> None:
    from medsupply import theme
    from medsupply.views import notices

    theme.inject_css()
    notices.render()


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼
# ---------------------------------------------------------------------------


def _generate_snapshot(db_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable, str(GENERATE_SCRIPT),
            "--baseline-only", "--seed", str(SEED), "--base-date", BASE_DATE,
            "--out", str(db_path),
        ],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _seed_notice_with_extraction(db_path: Path) -> tuple[str, str]:
    """공고 1건 + 추출(확인 필요) + 매핑 1건을 raw SQL로 직접 주입한다.

    --baseline-only 스냅샷은 공고를 적재하지 않으므로(scripts/load_notices.py는 별도
    단계) 정상 경로 테스트는 브리프 지시대로 픽스처 DB에 직접 INSERT한다.
    Returns (notice_id, item_id).
    """
    conn = sqlite3.connect(db_path)
    try:
        item_id = conn.execute(
            "SELECT item_id FROM items ORDER BY item_id LIMIT 1"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notices(notice_id, published_date, title, source, source_url,"
            " raw_text, notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _TEST_NOTICE_ID, "2026-07-20", _TEST_NOTICE_TITLE, "테스트기관",
                "https://example.invalid/notice/test-1",
                "테스트 사유로 공급이 중단됩니다.", "공급중단", "2026-07-20T09:00:00",
            ),
        )
        payload = {
            "product_names": ["테스트 품목"],
            "ingredient_names": ["테스트성분"],
            "reason": "원료 수급 차질",
            "halt_start_date": "2026-07-20",
            "expected_restart_date": None,
            "notice_type": "공급중단",
            "evidence_quotes": ["테스트 사유로 공급이 중단됩니다."],
        }
        conn.execute(
            "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
            " prompt_version, provider, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                _TEST_NOTICE_ID, json.dumps(payload, ensure_ascii=False), 0.55, "확인 필요",
                "notice_extract@v1", "anthropic", "claude-opus-5",
            ),
        )
        conn.execute(
            "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id,"
            " match_basis, needs_review) VALUES (?, ?, ?, ?, ?)",
            (_TEST_NOTICE_ID, item_id, None, "standard_code", 0),
        )
        conn.commit()
    finally:
        conn.close()
    return _TEST_NOTICE_ID, item_id


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("notices_live_base") / "base.db"
    _generate_snapshot(db_path)
    return db_path


@pytest.fixture()
def live_db(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


# ---------------------------------------------------------------------------
# 정상 스냅샷 경로
# ---------------------------------------------------------------------------


def test_notices_renders_real_snapshot_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_notice_with_extraction(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_notices)
    at.run()

    assert not at.exception
    table = at.dataframe[0].value
    assert _TEST_NOTICE_TITLE in set(table["제목"])
    assert any(_TEST_NOTICE_TITLE in exp.label for exp in at.expander)
    assert len(at.button) >= 1  # 상태가 '확인 필요'라 "확인 완료로 저장" 버튼이 노출된다.


# ---------------------------------------------------------------------------
# DB 부재 경로
# ---------------------------------------------------------------------------


def test_notices_missing_db_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_notices)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)


def test_notices_smoke_still_passes_with_no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """기존 test_views_smoke.py의 notices 스모크(DB 부재 → 경고 경로)가 여전히
    통과하는지 이 파일에서도 같은 조건으로 재확인한다(회귀 방지)."""
    missing = Path("/nonexistent/medsupply-m17-test/medsupply.db")
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_notices)
    at.run()

    assert not at.exception


# ---------------------------------------------------------------------------
# 확인 액션 — 버튼 클릭이 아니라 함수 직접 호출로 검증(브리프 명시)
# ---------------------------------------------------------------------------


def test_confirm_notice_transitions_status_and_bumps_data_version(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    notice_id, _item_id = _seed_notice_with_extraction(live_db)

    monkeypatch.setattr(settings, "DB_PATH", live_db)
    st.cache_data.clear()
    st.cache_resource.clear()

    before_version = inventory.current_data_version()

    notices_service.confirm_notice(notice_id)
    st.cache_data.clear()

    conn = sqlite3.connect(live_db)
    try:
        status = conn.execute(
            "SELECT status FROM notice_extractions WHERE notice_id = ?", (notice_id,)
        ).fetchone()[0]
    finally:
        conn.close()
    after_version = inventory.current_data_version()

    assert status == "확인 완료"
    assert after_version == before_version + 1
