"""Task M-23: review.py "AI 근거 설명" 탭(info_tab) 실데이터 연동 테스트.

AppTest.from_function(review.render 래퍼)을 온디스크 소형 표준 스냅샷(subprocess로
--baseline-only 생성 + 위험 평가 배치 1 run 실행)에 대해 실행한다(tests/platform/
test_review_live.py와 동일 관례). st.cache_data/st.cache_resource는 프로세스 전역이라
각 테스트에서 반드시 clear()해야 이전 테스트의 커넥션·결과가 재사용되지 않는다.

llm_explanations 저장분은 writer.save_explanation으로 픽스처 DB에 직접 시드한다(DB
수정 금지 — 실 데이터베이스가 아니라 함수별 tmp_path 복사본만 건드린다). API 키
존재 여부는 os.environ을 monkeypatch로 세팅/제거해 검증한다(브리프: "키 없는 현재
환경이 기본 경로").
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from medsupply import settings
from medsupply.data import writer
from medsupply.services import inventory

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"

SEED = 20260801
BASE_DATE = "2026-08-01"

_PENDING_NOTICE = "AI 원인 설명이 아직 생성되지 않았습니다."
_SCOPE_NOTE = "AI는 위험등급 판정에 관여하지 않습니다."
_GENERATE_LABEL = "설명 생성"


def _run_review() -> None:
    from medsupply import theme
    from medsupply.views import review

    theme.inject_css()
    review.render()


# ---------------------------------------------------------------------------
# 서브프로세스·DB 헬퍼(tests/platform/test_review_live.py와 동일 관례)
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


def _run_batch(db_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(BATCH_SCRIPT), "--db", str(db_path), "--as-of", BASE_DATE],
        capture_output=True, text=True, cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _activate_top_item(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """DB_PATH를 db_path로 바꾸고 캐시를 초기화한 뒤, selectbox가 기본 선택할(index 0,
    최고 점수) 품목의 item_id를 반환한다(review.py의 실제 호출부와 동일한 함수로 구해
    정렬 안정성 문제를 피한다 — tests/platform/test_review_live.py의 관례)."""
    monkeypatch.setattr(settings, "DB_PATH", db_path)
    st.cache_data.clear()
    st.cache_resource.clear()
    data_version = inventory.current_data_version()
    overview = inventory.load_overview(data_version=data_version)
    return overview.iloc[0]["item_id"]


def _latest_run_id(db_path: Path) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT run_id FROM risk_results ORDER BY as_of DESC, run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return row[0]


def _seed_explanation(
    db_path: Path,
    item_id: str,
    *,
    cause_summary: str,
    actions: list[dict],
    evidence_refs: list[str],
    hallucination_flags: list[str],
    provider: str = "anthropic",
    model: str = "claude-opus-5-test",
    prompt_version: str = "risk_explain@v1",
) -> str:
    """writer.save_explanation으로 픽스처 DB(함수별 tmp_path 복사본)에 저장분 1건을
    직접 시드한다. 저장된 generated_at(표시 텍스트 대조용)을 반환한다."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        run_id = _latest_run_id(db_path)
        payload = {
            "explanation": {
                "cause_summary": cause_summary,
                "actions": actions,
                "evidence_refs": evidence_refs,
                "history_note": None,
            },
            "hallucination_flags": hallucination_flags,
        }
        writer.save_explanation(
            conn, item_id, payload, prompt_version=prompt_version,
            provider=provider, model=model, run_id=run_id,
        )
        row = conn.execute(
            "SELECT generated_at FROM llm_explanations WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row["generated_at"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 픽스처
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def base_snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """--baseline-only 스냅샷 + 위험 평가 배치 1 run — 모듈 1회만 생성(비용 절감)."""
    db_path = tmp_path_factory.mktemp("review_explanation_base") / "base.db"
    _generate_snapshot(db_path)
    _run_batch(db_path)
    return db_path


@pytest.fixture()
def live_db(base_snapshot: Path, tmp_path: Path) -> Path:
    """base_snapshot을 함수별 tmp_path로 복사해 테스트 간 쓰기 격리를 보장한다."""
    dest = tmp_path / "t.db"
    shutil.copy(base_snapshot, dest)
    return dest


@pytest.fixture(autouse=True)
def _no_llm_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본 경로 = 키 없는 환경(브리프 명시). 호스트 환경에 실제 키가 있어도 결정적으로
    "미설정" 상태를 강제한다 — 키가 필요한 개별 테스트가 명시적으로 setenv한다."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


# ---------------------------------------------------------------------------
# ① 저장분 없음 — 정직한 안내(현재 기본 경로) + 생성 버튼 미노출(키 없음)
# ---------------------------------------------------------------------------


def test_no_stored_explanation_renders_pending_notice_without_exception(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_top_item(live_db, monkeypatch)

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert _PENDING_NOTICE in rendered
    assert "scripts/warm_cache.py" in rendered
    assert _SCOPE_NOTE in rendered
    # 키가 설정되지 않은 기본 환경이므로 생성 버튼 자체가 노출되지 않는다(disabled 아님).
    assert not any(b.label == _GENERATE_LABEL for b in at.button)


# ---------------------------------------------------------------------------
# ② 저장분 있음 + 플래그 0건 — cause_summary·actions 실데이터 렌더
# ---------------------------------------------------------------------------


def test_stored_explanation_without_flags_renders_summary_and_actions(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = _activate_top_item(live_db, monkeypatch)
    generated_at = _seed_explanation(
        live_db,
        item_id,
        cause_summary="최근 4주 사용량이 급증하고 공급중단 공고가 겹쳐 소진 위험이 상승했다.",
        actions=[
            {
                "title": "대체 가능 품목 재고 확인",
                "description": "동일 성분·함량 후보의 재고를 확인한다.",
                "evidence_refs": ["risk:x"],
            },
            {
                "title": "유통사 입고 일정 재확인",
                "description": "미확정 발주 건의 최단 입고일을 확인한다.",
                "evidence_refs": ["risk:x"],
            },
        ],
        evidence_refs=["risk:x", "usage:recent28", "stock:current"],
        hallucination_flags=[],
    )
    st.cache_data.clear()  # 시드 직후 캐시 재확인(무효화 신호는 data_version이지만 명시적으로).

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert "최근 4주 사용량이 급증하고 공급중단 공고가 겹쳐 소진 위험이 상승했다." in rendered
    assert "01 · 대체 가능 품목 재고 확인" in rendered
    assert "02 · 유통사 입고 일정 재확인" in rendered
    assert "동일 성분·함량 후보의 재고를 확인한다." in rendered
    assert "생성: anthropic/claude-opus-5-test" in rendered
    assert "프롬프트 risk_explain@v1" in rendered
    assert generated_at in rendered
    assert "근거 3건" in rendered
    assert _SCOPE_NOTE in rendered
    # 플래그가 없으므로 경고 배지는 나타나지 않는다.
    assert "사후 대조 경고" not in rendered


# ---------------------------------------------------------------------------
# ③ 저장분 있음 + 플래그 2건 — 경고 배지(부분 신호 문구, 플래그 비노출 금지)
# ---------------------------------------------------------------------------


def test_stored_explanation_with_flags_renders_warning_badge(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = _activate_top_item(live_db, monkeypatch)
    flags = ["unsupported_number: 500", "unknown_ref: notice:NTC-9999"]
    _seed_explanation(
        live_db,
        item_id,
        cause_summary="근거 밖 수치가 섞여 있을 수 있는 설명 예시.",
        actions=[
            {"title": "약사 재검토", "description": "본문과 근거를 대조 확인한다.", "evidence_refs": ["risk:x"]},
        ],
        evidence_refs=["risk:x"],
        hallucination_flags=flags,
    )
    st.cache_data.clear()

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    rendered = "\n".join(md.value for md in at.markdown)
    assert "사후 대조 경고 2건" in rendered
    assert "근거 밖 인용이 있을 수 있습니다" in rendered
    # 플래그를 숨기지 않는다 — 앞 2개 모두 본문에 그대로 드러나야 한다.
    for flag in flags:
        assert flag in rendered


# ---------------------------------------------------------------------------
# 생성 버튼 — 키 설정 시에만 노출(모킹)
# ---------------------------------------------------------------------------


def test_generate_button_visible_when_api_key_configured(
    live_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _activate_top_item(live_db, monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    matches = [b for b in at.button if b.label == _GENERATE_LABEL]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# 기존 review 스모크·라이브 테스트 회귀 방지(그린 유지 확인 — 별도 파일에서 이미 검증되지만
# 이 파일의 자동 delenv 픽스처가 그 결과에 영향을 주지 않음을 명시적으로 재확인한다)
# ---------------------------------------------------------------------------


def test_review_missing_db_still_shows_warning_without_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does_not_exist.db"
    assert not missing.exists()

    monkeypatch.setattr(settings, "DB_PATH", missing)
    st.cache_data.clear()
    st.cache_resource.clear()

    at = AppTest.from_function(_run_review)
    at.run()

    assert not at.exception
    assert len(at.warning) >= 1
    assert any("표준 스냅샷이 없습니다" in w.value for w in at.warning)
