"""Task M-29: scripts/measure_perf.py(성능 측정 하니스) 자체 검증.

task-M29-brief.md의 "산출물" 절이 요구하는 검증 범위는 셋뿐이다(브리프 원문:
"소형 픽스처로 스키마·판정 로직·결정성 아닌 통계 필드 존재 검증") — 실측 시간값
자체는 하드웨어·부하에 따라 매번 달라지는 게 당연하므로, 이 파일은 "몇 ms가
나오는가"가 아니라 "통계 필드가 다 채워지는가·판정 로직이 옳은가·스키마가
맞는가"만 검증한다(결정성 테스트는 의도적으로 두지 않는다).

1. 통계 헬퍼(`_percentile`)·판정 로직(`compute_verdict`) — 순수 함수 단위 테스트로
   손검산한다.
2. 소형 온디스크 픽스처로 `main()`을 실행해 출력 JSON의 스키마(대상 5종 × 통계
   필드 4종)와 값 타입·부호(0 이상의 float)만 확인한다.
3. 원본 DB 읽기 전용 — 실행 전후 픽스처 DB 바이트가 완전히 동일한지 확인한다
   (브리프: "원본 DB 읽기 전용(사본 불요 — 쓰기 없음 확인)").
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
from scripts import measure_perf as perf


@pytest.fixture(autouse=True)
def _restore_db_path() -> Iterator[None]:
    """run_measurement()은 settings.DB_PATH를 monkeypatch가 아니라 직접 대입한다
    (scripts/run_e2e.py와 동일 설계 — CLI 단독 프로세스 실행 전제). pytest는 전체
    스위트를 한 프로세스에서 실행하므로, 이 테스트 파일이 남긴 대입을 복원하지
    않으면 뒤에 실행되는 다른 테스트 모듈이 오염된 경로를 물려받는다(tests/
    platform/test_e2e_harness.py에서 실제로 겪은 회귀와 동일한 위험)."""
    original = settings.DB_PATH
    try:
        yield
    finally:
        settings.DB_PATH = original
        st.cache_data.clear()
        st.cache_resource.clear()


def _build_fixture_db(db_path: Path) -> None:
    """5개 측정 대상이 전부 실제로 동작할 수 있는 최소 온디스크 픽스처.

    tests/platform/test_e2e_harness.py의 픽스처와 동일한 최소 구성(품목 1·재고
    1행·공고 1건) — assess_snapshot이 위험 평가 배치 없이도(risk_results가
    비어 있어도) 전 품목을 산정할 수 있는 순수 계산 경로이므로 배치 실행은
    필요 없다.
    """
    conn = db.get_connection(str(db_path))
    db.init_db(conn, drop=False)
    conn.execute(
        "INSERT INTO items(item_id, item_name, supplier, is_essential) VALUES (?, ?, ?, ?)",
        ("ITM-PERF-1", "테스트품목", "테스트약품", 0),
    )
    conn.execute(
        "INSERT INTO stock_usage_daily(item_id, date, usage_qty, incoming_qty, closing_stock)"
        " VALUES (?, ?, ?, ?, ?)",
        ("ITM-PERF-1", "2026-08-01", 5, 0, 50),
    )
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, raw_text, notice_type)"
        " VALUES (?, ?, ?, ?, ?)",
        ("NTC-PERF-1", "2026-07-20", "테스트 공고", "테스트 원문입니다.", "공급중단"),
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
# 통계 헬퍼 — 순수 함수 손검산
# ---------------------------------------------------------------------------


class TestPercentile:
    def test_single_value_returns_itself(self) -> None:
        assert perf._percentile([42.0], 95) == 42.0

    def test_p50_of_odd_length_is_middle_value(self) -> None:
        assert perf._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 50) == 3.0

    def test_p100_is_max(self) -> None:
        assert perf._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 100) == 5.0

    def test_p0_is_min(self) -> None:
        assert perf._percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0) == 1.0

    def test_linear_interpolation_between_two_values(self) -> None:
        # rank = 0.5 * (2-1) = 0.5 → 1.0 + (2.0-1.0)*0.5 = 1.5
        assert perf._percentile([1.0, 2.0], 50) == 1.5

    def test_unsorted_input_is_sorted_first(self) -> None:
        assert perf._percentile([5.0, 1.0, 3.0], 50) == 3.0


class TestTimeRepeats:
    def test_excludes_warmup_call_from_stats(self) -> None:
        """워밍 1회 + repeats회를 합쳐 총 (1+repeats)번 호출되지만, 통계는 repeats개
        표본으로만 계산된다(개수를 세는 side effect로 간접 확인)."""
        call_count = 0

        def _tick() -> None:
            nonlocal call_count
            call_count += 1

        perf._time_repeats(_tick, repeats=5)

        assert call_count == 6  # 워밍 1 + 반복 5

    def test_returns_four_expected_keys(self) -> None:
        stats = perf._time_repeats(lambda: None, repeats=3)

        assert set(stats.keys()) == {"mean_ms", "p50_ms", "p95_ms", "max_ms"}
        for value in stats.values():
            assert isinstance(value, float)
            assert value >= 0.0

    def test_restores_gc_enabled_state_after_measurement(self) -> None:
        import gc

        assert gc.isenabled()
        perf._time_repeats(lambda: None, repeats=2)
        assert gc.isenabled()


# ---------------------------------------------------------------------------
# 판정 로직 — 순수 함수
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def test_all_targets_within_threshold_is_true(self) -> None:
        targets = {
            "a": {"p95_ms": 100.0}, "b": {"p95_ms": perf.P95_THRESHOLD_MS},
        }
        assert perf.compute_verdict(targets) is True

    def test_one_target_over_threshold_is_false(self) -> None:
        targets = {
            "a": {"p95_ms": 100.0}, "b": {"p95_ms": perf.P95_THRESHOLD_MS + 0.1},
        }
        assert perf.compute_verdict(targets) is False

    def test_empty_targets_is_false(self) -> None:
        assert perf.compute_verdict({}) is False


# ---------------------------------------------------------------------------
# 1회 실행 경로 — 소형 픽스처, 스키마·통계 필드 존재 검증(결정성은 검증하지 않는다)
# ---------------------------------------------------------------------------


class TestSchemaAndStatFields:
    def test_output_schema_and_stat_fields_are_complete(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "perf_results.json"

        rc = perf.main(["--db", str(fixture_db), "--repeats", "3", "--out", str(out_path)])

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert set(payload.keys()) == {
            "measured_at", "repeats", "db", "targets", "verdict", "environment",
        }
        assert payload["repeats"] == 3
        assert payload["db"] == str(fixture_db)

        assert set(payload["targets"].keys()) == {
            "list_items", "load_overview", "load_item_detail", "assess_snapshot",
            "notice_detail_sweep",
        }
        for name, stats in payload["targets"].items():
            assert set(stats.keys()) == {"mean_ms", "p50_ms", "p95_ms", "max_ms"}, name
            for key, value in stats.items():
                assert isinstance(value, float), f"{name}.{key}"
                assert value >= 0.0, f"{name}.{key}"

        assert isinstance(payload["verdict"], bool)
        assert payload["environment"]["python_version"]
        assert payload["environment"]["platform"]
        assert payload["measured_at"]

        # 소형 픽스처(품목 1)라면 어떤 하드웨어에서도 2초 판정선을 여유 있게
        # 통과해야 한다 — 이 부호는 "실측값이 그럴듯한가"의 최소 상식 검증이지,
        # 결정적 수치 손검산이 아니다.
        assert payload["verdict"] is True
        assert rc == 0

    def test_stdout_prints_human_summary(
        self, fixture_db: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        out_path = tmp_path / "perf_results.json"

        perf.main(["--db", str(fixture_db), "--repeats", "3", "--out", str(out_path)])

        captured = capsys.readouterr()
        assert captured.out.strip() != ""
        assert "판정" in captured.out
        assert "list_items" in captured.out

    def test_missing_db_exits_nonzero_without_writing_output(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.db"
        out_path = tmp_path / "perf_results.json"

        rc = perf.main(["--db", str(missing), "--repeats", "3", "--out", str(out_path)])

        assert rc == 1
        assert not out_path.exists()


# ---------------------------------------------------------------------------
# 원본 DB 읽기 전용 — 쓰기가 전혀 없음을 실측으로 확인
# ---------------------------------------------------------------------------


class TestReadOnly:
    """"쓰기 없음"의 실제 계약은 원본 DB의 내용(바이트)이 안 바뀌는 것이다 — WAL
    모드 DB는 읽기 전용 접근만으로도 -wal/-shm 사이드카가 리더 프로토콜상 생길 수
    있어(체크포인트는 쓰기 연결이 있어야 일어난다) 사이드카 부재까지는 단언하지
    않는다(scripts/measure_perf.py의 run_measurement 마지막 주석 참조). 실측으로
    확인해도 사이드카는 실제로 생기지만 내용은 항상 비어 있다 — 아래 해시 검증이
    그 실질(내용 불변)을 직접 증명한다.
    """

    def test_source_db_bytes_unchanged_after_measurement(
        self, fixture_db: Path, tmp_path: Path
    ) -> None:
        before_hash = _sha256(fixture_db)
        out_path = tmp_path / "perf_results.json"

        perf.main(["--db", str(fixture_db), "--repeats", "3", "--out", str(out_path)])

        after_hash = _sha256(fixture_db)
        assert after_hash == before_hash
