"""scripts/verify_reproducibility.py 하니스 단위 테스트(Task S-23).

브리프 범위 그대로: 실제 생성·배치·측정 subprocess 5회는 여기서 돌리지 않는다(느림 —
생성 1회만도 수십 초 걸린다). 소형 픽스처로 비교 로직(compare_values·compare_row_sets·
compare_json_dicts·compute_verdict)과 mismatch 검출, 그리고 sha256_of_json·
read_anchor_hash·_read_meta_value·_read_risk_results 같은 순수 I/O 헬퍼만 단위
검증한다(하니스 함수 단위 검증 — 브리프 "산출물" 절 그대로).

CLI 인자 배선은 무거운 실행 없이 즉시 실패하는 경로(필수 인자 누락·잘못된 날짜 형식)만
subprocess로 빠르게 스모크 검사한다 — argparse 검증은 실제 재현 로직을 타기 전에
끝나므로 밀리초 단위로 끝난다.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import verify_reproducibility as vr

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "verify_reproducibility.py"


# ---------------------------------------------------------------------------
# compare_values — content_hash 리스트·run_id 리스트 등 스칼라 값 비교
# ---------------------------------------------------------------------------


class TestCompareValues:
    def test_all_identical_returns_true_and_no_mismatch(self) -> None:
        identical, mismatch = vr.compare_values(["a", "a", "a"])
        assert identical is True
        assert mismatch is None

    def test_single_value_is_trivially_identical(self) -> None:
        identical, mismatch = vr.compare_values(["only"])
        assert identical is True
        assert mismatch is None

    def test_empty_list_is_trivially_identical(self) -> None:
        identical, mismatch = vr.compare_values([])
        assert identical is True
        assert mismatch is None

    def test_one_mismatch_detected_with_detail(self) -> None:
        identical, mismatch = vr.compare_values(["a", "a", "b"])
        assert identical is False
        assert mismatch is not None
        assert mismatch["reference"] == "a"
        assert mismatch["mismatches"] == [{"run": 2, "value": "b"}]

    def test_multiple_mismatches_all_reported(self) -> None:
        identical, mismatch = vr.compare_values(["a", "b", "c"])
        assert identical is False
        assert [m["run"] for m in mismatch["mismatches"]] == [1, 2]


# ---------------------------------------------------------------------------
# compare_row_sets — risk_results (item_id, grade, score, days_to_stockout) 집합 비교
# ---------------------------------------------------------------------------


class TestCompareRowSets:
    def test_identical_row_sets_across_runs(self) -> None:
        rows = [("I-001", "주의", 40, 10), ("I-002", "정상", 90, None)]
        identical, mismatch = vr.compare_row_sets([rows, list(rows), list(rows)])
        assert identical is True
        assert mismatch is None

    def test_order_independent(self) -> None:
        run_a = [("I-001", "주의", 40, 10), ("I-002", "정상", 90, None)]
        run_b = list(reversed(run_a))
        identical, mismatch = vr.compare_row_sets([run_a, run_b])
        assert identical is True
        assert mismatch is None

    def test_detects_differing_row(self) -> None:
        run_a = [("I-001", "주의", 40, 10)]
        run_b = [("I-001", "경고", 60, 5)]
        identical, mismatch = vr.compare_row_sets([run_a, run_b])
        assert identical is False
        assert mismatch["mismatches"][0]["run"] == 1
        assert ["I-001", "주의", 40, 10] in mismatch["mismatches"][0]["missing_from_run"]
        assert ["I-001", "경고", 60, 5] in mismatch["mismatches"][0]["extra_in_run"]

    def test_empty_list_is_trivially_identical(self) -> None:
        identical, mismatch = vr.compare_row_sets([])
        assert identical is True
        assert mismatch is None


# ---------------------------------------------------------------------------
# compare_json_dicts — measure_detection.py "results" 딕셔너리 비교
# ---------------------------------------------------------------------------


class TestCompareJsonDicts:
    def test_identical_dicts(self) -> None:
        d = {"detection_rate": 0.8, "counts": {"labeled": 4}}
        identical, mismatch = vr.compare_json_dicts([d, dict(d), dict(d)])
        assert identical is True
        assert mismatch is None

    def test_ignored_keys_do_not_break_equality(self) -> None:
        d1 = {"detection_rate": 0.8, "generated_at": "2026-08-21T00:00:00"}
        d2 = {"detection_rate": 0.8, "generated_at": "2026-08-21T00:05:00"}
        identical, mismatch = vr.compare_json_dicts([d1, d2], ignore_keys=("generated_at",))
        assert identical is True
        assert mismatch is None

    def test_detects_differing_key_without_ignoring(self) -> None:
        d1 = {"detection_rate": 0.8}
        d2 = {"detection_rate": 0.5}
        identical, mismatch = vr.compare_json_dicts([d1, d2])
        assert identical is False
        assert mismatch["mismatches"][0]["differing_keys"] == ["detection_rate"]

    def test_single_dict_is_trivially_identical(self) -> None:
        identical, mismatch = vr.compare_json_dicts([{"x": 1}])
        assert identical is True
        assert mismatch is None


# ---------------------------------------------------------------------------
# compute_verdict — 3계열 identical의 순수 AND
# ---------------------------------------------------------------------------


class TestComputeVerdict:
    def test_all_identical_true(self) -> None:
        assert (
            vr.compute_verdict(
                {"identical": True}, {"identical": True}, {"identical": True}
            )
            is True
        )

    def test_generation_false_makes_overall_false(self) -> None:
        assert (
            vr.compute_verdict(
                {"identical": False}, {"identical": True}, {"identical": True}
            )
            is False
        )

    def test_batch_false_makes_overall_false(self) -> None:
        assert (
            vr.compute_verdict(
                {"identical": True}, {"identical": False}, {"identical": True}
            )
            is False
        )

    def test_detection_false_makes_overall_false(self) -> None:
        assert (
            vr.compute_verdict(
                {"identical": True}, {"identical": True}, {"identical": False}
            )
            is False
        )


# ---------------------------------------------------------------------------
# sha256_of_json — 순서 무관 결정적 다이제스트
# ---------------------------------------------------------------------------


class TestSha256OfJson:
    def test_deterministic_regardless_of_key_order(self) -> None:
        assert vr.sha256_of_json({"b": 2, "a": 1}) == vr.sha256_of_json({"a": 1, "b": 2})

    def test_differs_for_different_object(self) -> None:
        assert vr.sha256_of_json({"a": 1}) != vr.sha256_of_json({"a": 2})

    def test_returns_64_char_hex(self) -> None:
        digest = vr.sha256_of_json([1, 2, 3])
        assert len(digest) == 64
        int(digest, 16)  # ValueError면 hex 문자열이 아니라는 뜻


# ---------------------------------------------------------------------------
# read_anchor_hash — standard_snapshot.sha256 포맷 파서(주석 1줄 이상 + 해시 1줄)
# ---------------------------------------------------------------------------


class TestReadAnchorHash:
    def test_reads_first_non_comment_line(self, tmp_path: Path) -> None:
        anchor = tmp_path / "anchor.sha256"
        anchor.write_text("# 생성 명령 주석\nabc123\n", encoding="utf-8")
        assert vr.read_anchor_hash(anchor) == "abc123"

    def test_skips_multiple_comment_lines(self, tmp_path: Path) -> None:
        anchor = tmp_path / "anchor.sha256"
        anchor.write_text("# 1행\n# 2행\ndeadbeef\n", encoding="utf-8")
        assert vr.read_anchor_hash(anchor) == "deadbeef"

    def test_raises_if_no_non_comment_line(self, tmp_path: Path) -> None:
        anchor = tmp_path / "anchor.sha256"
        anchor.write_text("# 주석뿐\n", encoding="utf-8")
        with pytest.raises(ValueError):
            vr.read_anchor_hash(anchor)

    def test_reads_repo_standard_snapshot_anchor_file(self) -> None:
        """실제 data/scenarios/standard_snapshot.sha256(읽기 전용 조회)도 파싱 가능해야
        한다 — 64자리 hex 형식 확인(내용 변경 없음, open()만 수행)."""
        digest = vr.read_anchor_hash(vr.STANDARD_ANCHOR_PATH)
        assert len(digest) == 64
        int(digest, 16)


# ---------------------------------------------------------------------------
# _read_meta_value / _read_risk_results — 소형 sqlite 픽스처(실제 스냅샷 생성 없음)
# ---------------------------------------------------------------------------


class TestReadDbHelpers:
    def test_read_meta_value_returns_stored_value(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fixture.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('content_hash', 'deadbeef')")
        conn.commit()
        conn.close()
        assert vr._read_meta_value(db_path, "content_hash") == "deadbeef"

    def test_read_meta_value_returns_none_if_missing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fixture.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.commit()
        conn.close()
        assert vr._read_meta_value(db_path, "content_hash") is None

    def test_read_risk_results_returns_run_id_and_sorted_rows(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fixture.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE risk_results (run_id TEXT, item_id TEXT, grade TEXT,"
            " score INTEGER, days_to_stockout INTEGER)"
        )
        conn.executemany(
            "INSERT INTO risk_results VALUES (?, ?, ?, ?, ?)",
            [
                ("R1", "I-002", "정상", 90, None),
                ("R1", "I-001", "주의", 40, 10),
            ],
        )
        conn.commit()
        conn.close()
        run_id, rows = vr._read_risk_results(db_path)
        assert run_id == "R1"
        assert rows == [("I-001", "주의", 40, 10), ("I-002", "정상", 90, None)]

    def test_read_risk_results_raises_on_multiple_run_ids(self, tmp_path: Path) -> None:
        db_path = tmp_path / "fixture.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE risk_results (run_id TEXT, item_id TEXT, grade TEXT,"
            " score INTEGER, days_to_stockout INTEGER)"
        )
        conn.executemany(
            "INSERT INTO risk_results VALUES (?, ?, ?, ?, ?)",
            [("R1", "I-001", "주의", 40, 10), ("R2", "I-002", "정상", 90, None)],
        )
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError):
            vr._read_risk_results(db_path)


# ---------------------------------------------------------------------------
# CLI 인자 배선 — 무거운 실행 없이 빠른 실패 경로만(argparse 검증은 즉시 종료)
# ---------------------------------------------------------------------------


class TestCliArgumentWiring:
    def test_missing_required_args_exits_2(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)], capture_output=True, text=True, cwd=REPO_ROOT
        )
        assert proc.returncode == 2

    def test_malformed_detection_date_exits_2(self) -> None:
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--out", "/tmp/verify_repro_should_not_be_created.json",
                "--labels", "somewhere.json",
                "--detection-start", "not-a-date",
                "--detection-end", "2026-08-01",
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode == 2

    def test_help_does_not_mention_ground_truth_literal(self) -> None:
        """소스에 정답 라벨 경로가 하드코딩돼 있지 않은지 실행 시점 표면에서도 재확인한다
        (tests/test_isolation.py의 정적 AST 검사와는 별개의 보강 확인 — --labels는 항상
        호출부가 CLI 인자로 넘겨야 하고, 이 스크립트 자체는 그 경로를 알지 못한다)."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, cwd=REPO_ROOT
        )
        assert proc.returncode == 0
        assert "ground_truth" not in proc.stdout
