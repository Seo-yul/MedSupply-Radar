"""Task M-28: scripts/run_e2e.py(E2E 하니스) 자체 검증.

task-M28-brief.md의 "산출물" 절이 요구하는 것은 하니스 자체의 검증이다 — 5단계
각각의 화면 렌더 세부 검증(예: situation.py의 KPI 표기, review.py의 라벨 카드)은
이미 tests/platform/test_situation_live.py 등 기존 test_*_live.py가 담당하므로,
여기서 다시 검증하지 않는다. 이 파일이 검증하는 것은 브리프 §하니스·§규칙이 요구한
세 가지다.

1. 1회 실행 경로 — 소형 온디스크 픽스처(품목 1·재고 1행·공고 1건)로 --runs 1을 돌려
   출력 JSON 스키마와 5단계 모두 통과를 확인한다.
2. 판정 로직 — passed_runs >= 9(마스터 플랜 고정값)를 순수 함수(compute_verdict)로
   직접 검증한다. 실행 경로 테스트는 runs=1(<9)이라 verdict가 구조적으로 항상 False가
   되므로, "9 이상이면 True"를 보이려면 이 순수 함수 단위 테스트가 별도로 필요하다.
3. 실패 주입 시 exit 1 — STEP_FUNCS 중 한 단계를 몽키패치로 예외를 던지게 바꾸고,
   그 단계만 passed=False로 기록되며 종료 코드가 1인지 확인한다(다른 단계는 계속
   실행되어 정상 기록되는지도 함께 확인한다).

추가로 브리프의 필수 불변식("원본 DB는 절대 수정하지 않는다")을 원본 파일 바이트
불변 검증으로 고정한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit as st

from medsupply import settings
from medsupply.data import db
from scripts import run_e2e as e2e


@pytest.fixture(autouse=True)
def _restore_db_path() -> Iterator[None]:
    """scripts/run_e2e.py의 _activate()는 settings.DB_PATH를 monkeypatch가 아니라
    직접 대입한다(CLI 단독 프로세스 실행을 전제한 설계 — 프로세스가 끝나면 자연히
    사라진다). 하지만 pytest는 전체 스위트를 한 프로세스에서 실행하므로, 이 대입을
    복원하지 않으면 이 파일 뒤에 실행되는 다른 테스트 모듈(예: test_views_smoke.py)이
    존재하지 않는 tmp 경로를 settings.DB_PATH로 물려받는 오염이 생긴다. 그래서 이
    테스트 파일 자신은 매 테스트 전후로 원래 값을 저장·복원한다(tests/platform/
    test_*_live.py가 monkeypatch로 얻는 것과 동일한 격리 효과).
    """
    original = settings.DB_PATH
    try:
        yield
    finally:
        settings.DB_PATH = original
        st.cache_data.clear()
        st.cache_resource.clear()


def _build_fixture_db(db_path: Path) -> None:
    """5단계 전부가 실제로 통과할 수 있는 최소 온디스크 픽스처.

    품목 1(공급사 포함 — 발주 요청안의 suppliers가 비지 않게), stock_usage_daily 1행
    (current_stock·avg_daily_usage 산출용), notices 1건(원문 포함 — 상세 로드 검증용).
    위험 평가 배치는 실행하지 않는다 — situation/review/history/alerts 렌더는 배치
    미실행 상태(risk_results 없음)에서도 "무예외"가 계약이므로(각 뷰의 "위험 평가
    배치를 실행하세요" info 경로) 이 최소 픽스처로 5단계 전부를 검증할 수 있다.
    """
    conn = db.get_connection(str(db_path))
    db.init_db(conn, drop=False)
    conn.execute(
        "INSERT INTO items(item_id, item_name, supplier, is_essential) VALUES (?, ?, ?, ?)",
        ("ITM-E2E-1", "테스트품목", "테스트약품", 0),
    )
    conn.execute(
        "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
        " VALUES (?, ?, ?, ?, ?)",
        ("ITM-E2E-1", "2026-08-01", 5, 0, 50),
    )
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, raw_text, notice_type)"
        " VALUES (?, ?, ?, ?, ?)",
        ("NTC-E2E-1", "2026-07-20", "테스트 공고", "테스트 원문입니다.", "공급중단"),
    )
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("base_date", "2026-08-01"), ("item_count", "1"), ("data_version", "1"), ("seed", "1")],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "fixture.db"
    _build_fixture_db(db_path)
    return db_path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# 판정 로직 — 순수 함수
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def test_nine_passes_meets_threshold(self) -> None:
        assert e2e.compute_verdict(9) is True

    def test_ten_passes_meets_threshold(self) -> None:
        assert e2e.compute_verdict(10) is True

    def test_eight_passes_is_below_threshold(self) -> None:
        assert e2e.compute_verdict(8) is False

    def test_zero_passes_is_below_threshold(self) -> None:
        assert e2e.compute_verdict(0) is False


# ---------------------------------------------------------------------------
# 1회 실행 경로 — 소형 픽스처, 5단계 전부 통과
# ---------------------------------------------------------------------------


class TestSingleRunHappyPath:
    def test_all_five_steps_pass_and_json_schema_is_complete(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "e2e_results.json"

        rc = e2e.main(["--db", str(fixture_db), "--runs", "1", "--out", str(out_path)])

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert set(payload.keys()) == {"runs", "per_run", "passed_runs", "verdict", "environment"}
        assert payload["runs"] == 1
        assert len(payload["per_run"]) == 1

        run = payload["per_run"][0]
        assert set(run.keys()) == {"run", "steps", "all_passed", "duration_ms"}
        assert run["run"] == 1
        assert set(run["steps"].keys()) == {
            "situation", "workbench", "notices", "action_order", "history_alerts",
        }
        for step_name, step_result in run["steps"].items():
            assert step_result["passed"] is True, f"{step_name} 실패: {step_result['error']}"
            assert step_result["error"] is None
            assert isinstance(step_result["duration_ms"], float)

        assert run["all_passed"] is True
        assert payload["passed_runs"] == 1
        # runs=1 < PASSED_RUNS_THRESHOLD(9) — 판정선은 조정하지 않으므로 verdict는
        # 구조적으로 False다(이 테스트는 "5단계가 실제로 전부 통과하는가"만 확인한다 —
        # verdict=True 경로는 아래 TestComputeVerdict가 순수 함수로 별도 검증한다).
        assert payload["verdict"] is False
        assert rc == 1

        env = payload["environment"]
        assert isinstance(env["llm_keys"], bool)
        assert env["python_version"]
        assert env["platform"]

    def test_stdout_prints_human_summary(
        self, fixture_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_path = tmp_path / "e2e_results.json"

        e2e.main(["--db", str(fixture_db), "--runs", "1", "--out", str(out_path)])

        captured = capsys.readouterr()
        assert captured.out.strip() != ""
        assert "회차 1" in captured.out
        assert "판정" in captured.out


# ---------------------------------------------------------------------------
# 실패 주입 — 한 단계를 몽키패치로 깨뜨려 exit 1을 확인
# ---------------------------------------------------------------------------


class TestFailureInjection:
    def test_broken_step_is_recorded_failed_and_exits_one(
        self, fixture_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(_db_path: Path) -> None:
            raise RuntimeError("주입된 실패")

        patched = [
            (name, _boom if name == "workbench" else func) for name, func in e2e.STEP_FUNCS
        ]
        monkeypatch.setattr(e2e, "STEP_FUNCS", patched)

        out_path = tmp_path / "e2e_results.json"
        rc = e2e.main(["--db", str(fixture_db), "--runs", "1", "--out", str(out_path)])

        assert rc == 1
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        run = payload["per_run"][0]

        assert run["steps"]["workbench"]["passed"] is False
        assert "주입된 실패" in run["steps"]["workbench"]["error"]
        assert run["all_passed"] is False
        # 실패한 단계와 무관한 앞선 단계는 계속 실행되어 정상 기록된다(브리프: 회차
        # 안에서 단계별 pass/fail을 각각 기록).
        assert run["steps"]["situation"]["passed"] is True
        assert payload["passed_runs"] == 0
        assert payload["verdict"] is False

    def test_missing_db_exits_nonzero_without_writing_output(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does_not_exist.db"
        out_path = tmp_path / "e2e_results.json"

        rc = e2e.main(["--db", str(missing), "--runs", "1", "--out", str(out_path)])

        assert rc == 1
        assert not out_path.exists()


# ---------------------------------------------------------------------------
# 원본 DB 불변 — 하니스가 --db를 절대 열어 쓰지 않는다
# ---------------------------------------------------------------------------


class TestOriginalDbUntouched:
    def test_source_db_bytes_unchanged_after_run(self, fixture_db: Path, tmp_path: Path) -> None:
        before_hash = _sha256(fixture_db)
        out_path = tmp_path / "e2e_results.json"

        e2e.main(["--db", str(fixture_db), "--runs", "2", "--out", str(out_path)])

        after_hash = _sha256(fixture_db)
        assert after_hash == before_hash

    def test_no_wal_or_shm_sidecar_left_next_to_source(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "e2e_results.json"

        e2e.main(["--db", str(fixture_db), "--runs", "1", "--out", str(out_path)])

        assert not fixture_db.with_name(fixture_db.name + "-wal").exists()
        assert not fixture_db.with_name(fixture_db.name + "-shm").exists()
