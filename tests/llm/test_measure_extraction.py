"""Task S-25: scripts/measure_extraction.py 공고 추출 정확도 측정 CLI 테스트.

task-S25-brief.md 계약을 검증한다. 세 계층으로 나뉜다.

- 순수 함수 단위 테스트(normalize_name 이하): DB·CLI 없이 정규화·비교 산식만 검증한다.
  집합 정규화(casefold+공백 제거), 자카드, N-001·N-014·N-017 양가 인정 허용 목록,
  reason 토큰 포함 여부를 각각 손검산으로 고정한다.
- TestBuildReport: 파이썬 dict(골드 4~5건 + 추출 4건)로 build_report의 필드별 정확도·
  needs_review 재현율/정밀도·macro_accuracy·per_notice(unextracted 포함)를 손검산으로
  고정한다(DB 미개입).
- TestCLI: tmp 파일 DB + subprocess/인프로세스로 CLI 계약(0행 exit 1·파일 미생성, 정상
  경로 exit 0·스키마 완비, 결정성)을 검증한다.

scripts/measure_extraction.py는 tests/test_isolation.py의 GOLD_LABELS_PATH_ALLOWLIST가
이미 문서화해 둔 대로(Task S-24) SCRIPTS_PATH_TARGETS에 등록하지 않고 격리 스캔 전면
제외 대상으로 남긴다(scripts/measure_detection.py와 동급) — 이 파일은 그 상태를 바꾸지
않는다.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from medsupply.data import db
from scripts import measure_extraction as me

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "measure_extraction.py"


def _payload(
    *,
    product_names: list[str],
    ingredient_names: list[str],
    reason: str,
    halt_start_date: str | None,
    expected_restart_date: str | None,
    notice_type: str,
) -> dict:
    return {
        "product_names": product_names,
        "ingredient_names": ingredient_names,
        "reason": reason,
        "halt_start_date": halt_start_date,
        "expected_restart_date": expected_restart_date,
        "notice_type": notice_type,
        "evidence_quotes": [],
    }


# ---------------------------------------------------------------------------
# normalize_name / normalize_name_set — casefold + 공백(전체) 제거
# ---------------------------------------------------------------------------


class TestNormalizeName:
    def test_casefolds_and_strips_all_whitespace(self) -> None:
        assert me.normalize_name(" ABC 정\t10mg ") == "abc정10mg"

    def test_internal_spaces_removed_not_just_edges(self) -> None:
        assert me.normalize_name("아 지 트 로 마 이 신") == "아지트로마이신"

    def test_set_normalizes_each_element(self) -> None:
        assert me.normalize_name_set([" A B ", "c D"]) == {"ab", "cd"}


# ---------------------------------------------------------------------------
# jaccard — 완전 일치·부분 겹침·양쪽 공집합
# ---------------------------------------------------------------------------


class TestJaccard:
    def test_identical_sets_is_one(self) -> None:
        assert me.jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_partial_overlap(self) -> None:
        # 교집합 {a} 1개, 합집합 {a,b,c} 3개 -> 1/3
        assert me.jaccard({"a", "b"}, {"a", "c"}) == pytest.approx(1 / 3)

    def test_disjoint_sets_is_zero(self) -> None:
        assert me.jaccard({"a"}, {"b"}) == 0.0

    def test_both_empty_is_one_by_convention(self) -> None:
        assert me.jaccard(set(), set()) == 1.0


# ---------------------------------------------------------------------------
# expected_restart_date_matches — N-001/N-014/N-017 양가 인정 허용 목록
# ---------------------------------------------------------------------------


class TestExpectedRestartDateAllowlist:
    def test_exact_match_without_allowlist(self) -> None:
        assert me.expected_restart_date_matches("N-999", "2026-01-01", "2026-01-01") is True

    def test_mismatch_without_allowlist(self) -> None:
        assert me.expected_restart_date_matches("N-999", "2026-01-01", "2026-01-02") is False

    def test_n001_accepts_alternate_date(self) -> None:
        # 골드 채택값(11-20) 아닌, notes에 병기된 대안(11-05)도 정답 인정.
        assert me.expected_restart_date_matches("N-001", "2024-11-20", "2024-11-05") is True

    def test_n001_still_accepts_canonical_value(self) -> None:
        assert me.expected_restart_date_matches("N-001", "2024-11-20", "2024-11-20") is True

    def test_n001_rejects_unrelated_value(self) -> None:
        # 허용 목록은 지정된 대안 값만 인정한다 — 임의의 다른 날짜는 여전히 오답.
        assert me.expected_restart_date_matches("N-001", "2024-11-20", "2024-12-01") is False

    def test_n014_accepts_alternate_date(self) -> None:
        assert me.expected_restart_date_matches("N-014", "2025-08-11", "2025-05-12") is True

    def test_n017_accepts_alternate_date(self) -> None:
        assert me.expected_restart_date_matches("N-017", "2026-01-20", "2026-01-30") is True

    def test_allowlist_is_scoped_to_its_own_notice_id(self) -> None:
        # N-001의 대안값이 다른 notice_id에는 적용되지 않는다(허용 목록은 notice_id별).
        assert me.expected_restart_date_matches("N-002", "2024-11-20", "2024-11-05") is False

    def test_both_null_matches(self) -> None:
        assert me.expected_restart_date_matches("N-999", None, None) is True


# ---------------------------------------------------------------------------
# reason 토큰 포함 여부(참고 지표 reason_overlap_rate의 단위 판정)
# ---------------------------------------------------------------------------


class TestReasonOverlap:
    def test_true_when_any_gold_token_contained(self) -> None:
        assert me.reason_overlap("설비 점검으로 공급 중단", "설비 점검으로 인한 공급 중단") is True

    def test_false_when_no_gold_token_contained(self) -> None:
        assert me.reason_overlap("채산성 악화", "설비 노후화") is False

    def test_case_insensitive(self) -> None:
        assert me.reason_overlap("Supply HALT", "supply halt due to plant issue") is True

    def test_empty_extracted_reason_is_false(self) -> None:
        assert me.reason_overlap("공급 중단", "") is False


# ---------------------------------------------------------------------------
# build_report — 손검산(골드 5건: 추출 4건 + unextracted 1건)
# ---------------------------------------------------------------------------
#
# N-101: 전 필드 일치 + status=자동확정 -> needs_review TN
# N-102: notice_type 불일치 + product_names 부분 겹침(자카드 1/3) + status=확인 필요 -> TP
# N-103: halt_start_date 불일치 + status=자동확정 -> FN(미탐)
# N-104: 전 필드 일치 + status=확인 필요 -> FP(과탐)
# N-105: 골드에만 존재(추출 없음) -> unextracted


_GOLD = {
    "N-101": {
        "product_names": ["가나다정"],
        "ingredient_names": ["가나다"],
        "reason": "설비 점검으로 공급 중단",
        "halt_start_date": "2026-01-01",
        "expected_restart_date": None,
        "notice_type": "공급중단",
    },
    "N-102": {
        "product_names": ["ABC정", "DEF정"],
        "ingredient_names": ["에이비씨"],
        "reason": "원료 수급 지연",
        "halt_start_date": "2026-02-01",
        "expected_restart_date": "2026-03-01",
        "notice_type": "공급부족",
    },
    "N-103": {
        "product_names": ["가나다정"],
        "ingredient_names": ["가나다"],
        "reason": "채산성 악화",
        "halt_start_date": "2026-03-15",
        "expected_restart_date": None,
        "notice_type": "공급중단",
    },
    "N-104": {
        "product_names": ["ABC정"],
        "ingredient_names": ["에이비씨"],
        "reason": "생산 지연",
        "halt_start_date": "2026-04-01",
        "expected_restart_date": "2026-05-01",
        "notice_type": "공급부족",
    },
    "N-105": {
        "product_names": ["GHI정"],
        "ingredient_names": ["지에이치아이"],
        "reason": "기타 사유",
        "halt_start_date": "2026-06-01",
        "expected_restart_date": None,
        "notice_type": "기타",
    },
}

_EXTRACTIONS = {
    "N-101": {
        "payload": _payload(
            product_names=["가나다정"], ingredient_names=["가나다"],
            reason="설비 점검으로 인한 공급 중단", halt_start_date="2026-01-01",
            expected_restart_date=None, notice_type="공급중단",
        ),
        "status": "자동확정",
    },
    "N-102": {
        "payload": _payload(
            product_names=["ABC정", "XYZ정"], ingredient_names=["에이비씨"],
            reason="주요 원료 수급이 지연됨", halt_start_date="2026-02-01",
            expected_restart_date="2026-03-01", notice_type="공급중단",  # notice_type 불일치
        ),
        "status": "확인 필요",
    },
    "N-103": {
        "payload": _payload(
            product_names=["가나다정"], ingredient_names=["가나다"],
            reason="설비 노후화", halt_start_date="2026-03-16",  # halt_start_date 불일치
            expected_restart_date=None, notice_type="공급중단",
        ),
        "status": "자동확정",
    },
    "N-104": {
        "payload": _payload(
            product_names=["ABC정"], ingredient_names=["에이비씨"],
            reason="생산 라인 지연", halt_start_date="2026-04-01",
            expected_restart_date="2026-05-01", notice_type="공급부족",
        ),
        "status": "확인 필요",
    },
}


class TestBuildReportFieldAccuracy:
    @staticmethod
    @pytest.fixture(scope="class")
    def report() -> dict:
        return me.build_report(_GOLD, _EXTRACTIONS)

    def test_extracted_and_unextracted_counts(self, report: dict) -> None:
        assert report["extracted_count"] == 4
        assert report["unextracted_count"] == 1

    def test_notice_type_accuracy(self, report: dict) -> None:
        # N-102만 불일치 -> 3/4
        field = report["per_field"]["notice_type"]
        assert field["matched"] == 3
        assert field["total"] == 4
        assert field["accuracy"] == pytest.approx(0.75)

    def test_halt_start_date_accuracy(self, report: dict) -> None:
        # N-103만 불일치 -> 3/4
        field = report["per_field"]["halt_start_date"]
        assert field["matched"] == 3
        assert field["accuracy"] == pytest.approx(0.75)

    def test_expected_restart_date_accuracy_all_match(self, report: dict) -> None:
        field = report["per_field"]["expected_restart_date"]
        assert field["matched"] == 4
        assert field["accuracy"] == pytest.approx(1.0)

    def test_product_names_exact_match_and_jaccard(self, report: dict) -> None:
        field = report["per_field"]["product_names"]
        # N-102만 불일치({abc정,def정} vs {abc정,xyz정}) -> 3/4 exact
        assert field["matched"] == 3
        assert field["exact_match_rate"] == pytest.approx(0.75)
        # 자카드 평균: (1 + 1/3 + 1 + 1) / 4
        assert field["jaccard_mean"] == pytest.approx((1 + 1 / 3 + 1 + 1) / 4, abs=1e-4)

    def test_ingredient_names_all_match(self, report: dict) -> None:
        field = report["per_field"]["ingredient_names"]
        assert field["matched"] == 4
        assert field["exact_match_rate"] == pytest.approx(1.0)
        assert field["jaccard_mean"] == pytest.approx(1.0)

    def test_reason_overlap_rate_is_reference_only(self, report: dict) -> None:
        # N-101 True, N-102 True, N-103 False("채산성 악화" 미포함), N-104 True -> 3/4
        field = report["per_field"]["reason"]
        assert field["matched"] == 3
        assert field["reason_overlap_rate"] == pytest.approx(0.75)

    def test_macro_accuracy_excludes_jaccard_and_reason(self, report: dict) -> None:
        # (0.75 + 0.75 + 1.0 + 0.75 + 1.0) / 5 = 0.85
        assert report["macro_accuracy"] == pytest.approx(0.85)


class TestBuildReportNeedsReview:
    @staticmethod
    @pytest.fixture(scope="class")
    def report() -> dict:
        return me.build_report(_GOLD, _EXTRACTIONS)

    def test_confusion_counts(self, report: dict) -> None:
        nr = report["needs_review"]
        assert nr["tp"] == 1  # N-102
        assert nr["fn"] == 1  # N-103
        assert nr["fp"] == 1  # N-104
        assert nr["tn"] == 1  # N-101

    def test_recall_and_precision(self, report: dict) -> None:
        nr = report["needs_review"]
        assert nr["recall"] == pytest.approx(0.5)
        assert nr["precision"] == pytest.approx(0.5)

    def test_misses_lists_fn_notice(self, report: dict) -> None:
        misses = report["needs_review"]["misses"]
        assert len(misses) == 1
        assert misses[0]["notice_id"] == "N-103"
        assert misses[0]["status"] == "자동확정"
        assert misses[0]["mismatched_fields"] == ["halt_start_date"]

    def test_false_alarms_lists_fp_notice(self, report: dict) -> None:
        false_alarms = report["needs_review"]["false_alarms"]
        assert len(false_alarms) == 1
        assert false_alarms[0]["notice_id"] == "N-104"
        assert false_alarms[0]["status"] == "확인 필요"


class TestBuildReportPerNotice:
    @staticmethod
    @pytest.fixture(scope="class")
    def report() -> dict:
        return me.build_report(_GOLD, _EXTRACTIONS)

    def test_per_notice_covers_all_gold_ids_sorted(self, report: dict) -> None:
        ids = [row["notice_id"] for row in report["per_notice"]]
        assert ids == ["N-101", "N-102", "N-103", "N-104", "N-105"]

    def test_all_match_notice_has_empty_mismatch_list(self, report: dict) -> None:
        by_id = {row["notice_id"]: row for row in report["per_notice"]}
        assert by_id["N-101"]["mismatched_fields"] == []
        assert by_id["N-104"]["mismatched_fields"] == []

    def test_mismatch_field_order_follows_scored_fields(self, report: dict) -> None:
        by_id = {row["notice_id"]: row for row in report["per_notice"]}
        assert by_id["N-102"]["mismatched_fields"] == ["notice_type", "product_names"]
        assert by_id["N-103"]["mismatched_fields"] == ["halt_start_date"]

    def test_unextracted_notice_uses_sentinel(self, report: dict) -> None:
        by_id = {row["notice_id"]: row for row in report["per_notice"]}
        assert by_id["N-105"]["mismatched_fields"] == ["unextracted"]


class TestBuildReportAllUnextracted:
    def test_zero_extracted_yields_null_rates_not_exceptions(self) -> None:
        report = me.build_report({"N-201": _GOLD["N-101"]}, {})
        assert report["extracted_count"] == 0
        assert report["unextracted_count"] == 1
        assert report["per_field"]["notice_type"]["accuracy"] is None
        assert report["per_field"]["product_names"]["jaccard_mean"] is None
        assert report["macro_accuracy"] is None
        assert report["needs_review"]["recall"] is None
        assert report["needs_review"]["precision"] is None
        assert report["per_notice"] == [{"notice_id": "N-201", "mismatched_fields": ["unextracted"]}]


class TestBuildReportAllowlistEndToEnd:
    def test_n001_alternate_date_not_flagged_as_mismatch(self) -> None:
        gold = {
            "N-001": {
                "product_names": ["약품A"],
                "ingredient_names": ["성분A"],
                "reason": "사유",
                "halt_start_date": "2024-10-17",
                "expected_restart_date": "2024-11-20",
                "notice_type": "공급부족",
            }
        }
        extractions = {
            "N-001": {
                "payload": _payload(
                    product_names=["약품A"], ingredient_names=["성분A"], reason="사유",
                    halt_start_date="2024-10-17",
                    expected_restart_date="2024-11-05",  # notes 병기 대안값
                    notice_type="공급부족",
                ),
                "status": "자동확정",
            }
        }
        report = me.build_report(gold, extractions)
        assert report["per_field"]["expected_restart_date"]["accuracy"] == pytest.approx(1.0)
        assert report["per_notice"][0]["mismatched_fields"] == []
        assert report["needs_review"]["tn"] == 1


# ---------------------------------------------------------------------------
# CLI — 0행 exit 1 / 정상 exit 0 / 결정성
# ---------------------------------------------------------------------------


def _init_tmp_db(db_path: Path) -> None:
    conn = db.get_connection(str(db_path))
    db.init_db(conn, drop=False)
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", ("content_hash", "deadbeef"))
    conn.commit()
    conn.close()


def _insert_notice_and_extraction(
    db_path: Path, notice_id: str, payload: dict, status: str
) -> None:
    conn = db.get_connection(str(db_path))
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, notice_type)"
        " VALUES (?, ?, ?, ?)",
        (notice_id, "2026-01-01", f"{notice_id} 공고", payload["notice_type"]),
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id, json.dumps(payload, ensure_ascii=False), 0.9, status,
            "notice_extract@v1", "anthropic", "claude-opus-5",
        ),
    )
    conn.commit()
    conn.close()


def _write_gold(path: Path, labels: dict) -> None:
    path.write_text(json.dumps({"version": "v1", "labels": labels}, ensure_ascii=False), encoding="utf-8")


class TestCLIZeroRowExit:
    def test_exits_one_with_stderr_hint_and_no_file_when_extractions_empty(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "empty.db"
        _init_tmp_db(db_path)  # notice_extractions 스키마만 있고 0행

        gold_path = tmp_path / "gold.json"
        _write_gold(gold_path, {"N-101": _GOLD["N-101"]})

        out_path = tmp_path / "extraction_accuracy.json"

        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--db", str(db_path), "--gold", str(gold_path), "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode == 1
        assert "process_notices.py --all" in proc.stderr
        assert not out_path.exists()

    def test_nonzero_table_but_no_gold_id_overlap_still_succeeds(self, tmp_path: Path) -> None:
        """notice_extractions가 0행은 아니지만(비-골드 notice만 처리됨) 골드 notice_id와
        하나도 안 겹치는 전환기 상태 — exit 1 대상이 아니라 extracted_count=0인 정상
        보고서로 처리돼야 한다(0행 사전조건은 테이블 전체 기준이지 골드 교집합 기준이
        아니다)."""
        db_path = tmp_path / "partial.db"
        _init_tmp_db(db_path)
        _insert_notice_and_extraction(
            db_path, "N-999", _EXTRACTIONS["N-101"]["payload"], "자동확정"
        )

        gold_path = tmp_path / "gold.json"
        _write_gold(gold_path, {"N-101": _GOLD["N-101"]})

        out_path = tmp_path / "extraction_accuracy.json"
        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--db", str(db_path), "--gold", str(gold_path), "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(out_path.read_text(encoding="utf-8"))
        assert payload["extracted_count"] == 0
        assert payload["unextracted_count"] == 1
        assert payload["macro_accuracy"] is None
        assert payload["needs_review"]["recall"] is None
        assert payload["per_field"]["notice_type"]["accuracy"] is None


class TestCLIHappyPath:
    @pytest.fixture()
    def populated_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "populated.db"
        _init_tmp_db(db_path)
        for notice_id, entry in _EXTRACTIONS.items():
            _insert_notice_and_extraction(db_path, notice_id, entry["payload"], entry["status"])
        return db_path

    @pytest.fixture()
    def gold_path(self, tmp_path: Path) -> Path:
        path = tmp_path / "gold.json"
        _write_gold(path, _GOLD)
        return path

    def test_cli_exits_zero_and_writes_complete_schema(
        self, populated_db: Path, gold_path: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "extraction_accuracy.json"

        proc = subprocess.run(
            [
                sys.executable, str(SCRIPT),
                "--db", str(populated_db), "--gold", str(gold_path), "--out", str(out_path),
            ],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )

        assert proc.returncode == 0, proc.stdout + proc.stderr
        payload = json.loads(out_path.read_text(encoding="utf-8"))

        assert set(payload.keys()) == {
            "measured_at", "db", "gold_version", "dataset_content_hash",
            "extracted_count", "unextracted_count", "per_field", "needs_review",
            "macro_accuracy", "per_notice",
        }
        assert payload["gold_version"] == "v1"
        assert payload["dataset_content_hash"] == "deadbeef"
        assert payload["extracted_count"] == 4
        assert payload["unextracted_count"] == 1
        assert payload["macro_accuracy"] == pytest.approx(0.85)
        assert proc.stdout.strip() != ""  # 사람이 읽는 요약이 stdout에 찍힌다

    def test_missing_required_args_fails_without_writing(
        self, populated_db: Path, tmp_path: Path
    ) -> None:
        out_path = tmp_path / "should_not_exist.json"
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(populated_db), "--out", str(out_path)],
            capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert proc.returncode != 0
        assert not out_path.exists()


class TestCLIDeterminism:
    """브리프 §결정성: 동일 입력 -> 동일 출력(measured_at 제외). main(argv) 인프로세스 호출."""

    def test_two_runs_produce_identical_output_except_measured_at(self, tmp_path: Path) -> None:
        db_path = tmp_path / "det.db"
        _init_tmp_db(db_path)
        for notice_id, entry in _EXTRACTIONS.items():
            _insert_notice_and_extraction(db_path, notice_id, entry["payload"], entry["status"])

        gold_path = tmp_path / "gold.json"
        _write_gold(gold_path, _GOLD)

        out1, out2 = tmp_path / "run1.json", tmp_path / "run2.json"
        rc1 = me.main(["--db", str(db_path), "--gold", str(gold_path), "--out", str(out1)])
        rc2 = me.main(["--db", str(db_path), "--gold", str(gold_path), "--out", str(out2)])

        assert rc1 == 0
        assert rc2 == 0
        payload1 = json.loads(out1.read_text(encoding="utf-8"))
        payload2 = json.loads(out2.read_text(encoding="utf-8"))
        payload1.pop("measured_at")
        payload2.pop("measured_at")
        assert payload1 == payload2
