"""eval/build_cases.py 테스트(Task S-26) — 선정 규칙·파일럿·결정성·메타·config 해시 일치.

선정·파일럿 로직(select_case_rows·select_pilot_ids·params_hash_from_run_id)은 DB 없이
DataFrame/문자열만으로 검증한다(순수 함수). build_dataset()은 :memory: 스키마 위의 최소
시드로 collect_risk_evidence(M-20)·history 추림(_trim_history, explain_item과 동일 규칙)이
실제로 결합되는지 검증한다. 끝의 TestRealArtifactsConsistency만 실제 커밋 산출물
(eval/cases/eval_cases_v1.json·eval/config.yaml)을 직접 대조한다.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
import yaml

from eval import build_cases
from medsupply.data import db, queries
from medsupply.llm.explanation import _HISTORY_LIMIT, _trim_history
from medsupply.llm.grounding import collect_risk_evidence

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_PATH = REPO_ROOT / "eval" / "cases" / "eval_cases_v1.json"
CONFIG_PATH = REPO_ROOT / "eval" / "config.yaml"

RUN_ID = "2026-08-01#a1b2c3d4"
AS_OF = "2026-08-01"


def _risk_df(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """(item_id, grade, risk_type) 목록 -> select_case_rows/select_pilot_ids가 참조하는
    최소 컬럼(get_risk_results 반환 형태의 부분집합)의 DataFrame."""
    return pd.DataFrame(
        [{"item_id": item_id, "grade": grade, "risk_type": risk_type} for item_id, grade, risk_type in rows]
    )


# ---------------------------------------------------------------------------
# select_case_rows — 등급 우선순위 규칙(순수 함수, DB 불필요)
# ---------------------------------------------------------------------------


class TestSelectCaseRows:
    def test_priority_grades_always_fully_included(self):
        rows = [(f"ITM-{i:04d}", "위험", "x") for i in range(1, 6)] + [
            (f"ITM-{i:04d}", "경고", "x") for i in range(6, 8)
        ]
        selected = build_cases.select_case_rows(_risk_df(rows))
        assert len(selected) == 7
        assert list(selected["item_id"]) == [f"ITM-{i:04d}" for i in range(1, 8)]

    def test_priority_grades_included_even_beyond_target_count(self):
        """위험·경고 등급은 "전건 포함" — TARGET_CASE_COUNT를 넘어도 잘라내지 않는다."""
        rows = [(f"ITM-{i:04d}", "위험", "x") for i in range(1, 45)]  # 44건 > 40
        selected = build_cases.select_case_rows(_risk_df(rows))
        assert len(selected) == 44

    def test_fills_from_caution_grade_ascending_until_target(self):
        priority = [(f"ITM-{i:04d}", "위험", "x") for i in range(1, 3)]  # 2건
        caution = [(f"ITM-{i:04d}", "주의", "x") for i in range(100, 150)]  # 50건(목표 초과분 존재)
        selected = build_cases.select_case_rows(_risk_df(priority + caution))

        assert len(selected) == build_cases.TARGET_CASE_COUNT
        expected_caution = [f"ITM-{i:04d}" for i in range(100, 100 + 38)]  # 40 - 2 = 38
        assert list(selected["item_id"]) == [f"ITM-{i:04d}" for i in range(1, 3)] + expected_caution

    def test_normal_grade_never_included(self):
        rows = [("ITM-0001", "위험", "x"), ("ITM-0002", "정상", "x"), ("ITM-0003", "주의", "x")]
        selected = build_cases.select_case_rows(_risk_df(rows))
        assert set(selected["item_id"]) == {"ITM-0001", "ITM-0003"}

    def test_under_target_when_grades_insufficient_no_padding(self):
        rows = [("ITM-0001", "위험", "x"), ("ITM-0002", "경고", "x"), ("ITM-0003", "주의", "x")]
        selected = build_cases.select_case_rows(_risk_df(rows))
        assert len(selected) == 3  # 40에 못 미쳐도 억지로 채우지 않는다

    def test_output_sorted_by_item_id_ascending(self):
        rows = [("ITM-0009", "위험", "x"), ("ITM-0001", "경고", "x"), ("ITM-0005", "주의", "x")]
        selected = build_cases.select_case_rows(_risk_df(rows))
        assert list(selected["item_id"]) == ["ITM-0001", "ITM-0005", "ITM-0009"]


# ---------------------------------------------------------------------------
# select_pilot_ids — 파일럿 4건(유형 대표 + 미충족 시 목록 선두 보충)
# ---------------------------------------------------------------------------


class TestSelectPilotIds:
    def test_four_distinct_types_each_min_item_id(self):
        rows = [
            ("ITM-0010", "위험", "typeA"),
            ("ITM-0020", "위험", "typeA"),
            ("ITM-0005", "주의", "typeB"),
            ("ITM-0002", "주의", "typeC"),
            ("ITM-0030", "주의", "typeD"),
            ("ITM-0001", "주의", "typeD"),
        ]
        pilot = build_cases.select_pilot_ids(_risk_df(rows))
        assert pilot == ["ITM-0001", "ITM-0002", "ITM-0005", "ITM-0010"]

    def test_more_than_four_types_keeps_four_smallest_representatives(self):
        rows = [
            ("ITM-0001", "위험", "A"),
            ("ITM-0002", "위험", "B"),
            ("ITM-0003", "위험", "C"),
            ("ITM-0004", "위험", "D"),
            ("ITM-0005", "위험", "E"),  # 대표 item_id가 가장 커서 탈락해야 함
        ]
        pilot = build_cases.select_pilot_ids(_risk_df(rows))
        assert pilot == ["ITM-0001", "ITM-0002", "ITM-0003", "ITM-0004"]

    def test_fewer_than_four_types_pads_from_head_of_list(self):
        """실 표준 DB(2026-08-01 run)와 동일 패턴 — general/delivery_delay 2유형뿐."""
        rows = [
            ("ITM-0001", "주의", "general"),
            ("ITM-0004", "주의", "general"),
            ("ITM-0006", "주의", "general"),
            ("ITM-0009", "주의", "general"),
            ("ITM-0036", "경고", "delivery_delay"),
        ]
        pilot = build_cases.select_pilot_ids(_risk_df(rows))
        # 대표 2건(ITM-0001 general, ITM-0036 delivery_delay) + 목록 선두 미포함분 2건 보충
        assert pilot == ["ITM-0001", "ITM-0004", "ITM-0006", "ITM-0036"]

    def test_single_type_pads_three_more_from_head(self):
        rows = [(f"ITM-{i:04d}", "주의", "general") for i in range(1, 6)]
        pilot = build_cases.select_pilot_ids(_risk_df(rows))
        assert pilot == ["ITM-0001", "ITM-0002", "ITM-0003", "ITM-0004"]

    def test_fewer_than_pilot_count_total_cases_returns_all(self):
        rows = [("ITM-0001", "위험", "A"), ("ITM-0002", "경고", "B")]
        pilot = build_cases.select_pilot_ids(_risk_df(rows))
        assert pilot == ["ITM-0001", "ITM-0002"]


# ---------------------------------------------------------------------------
# params_hash_from_run_id
# ---------------------------------------------------------------------------


class TestParamsHashFromRunId:
    def test_extracts_suffix_after_hash(self):
        assert build_cases.params_hash_from_run_id("2026-08-01#6ec9bf05") == "6ec9bf05"

    def test_none_when_no_hash_separator(self):
        assert build_cases.params_hash_from_run_id("2026-08-01") is None


# ---------------------------------------------------------------------------
# render_updated_dataset_section — eval/config.yaml dataset: 블록 갱신(순수 문자열 함수)
# ---------------------------------------------------------------------------


class TestRenderUpdatedDatasetSection:
    SAMPLE = (
        "rubric_version: v1\n"
        "prompt_version: judge_v1\n"
        "dataset:\n"
        "  cases: 40\n"
        "  pilot: 4\n"
        "  content_hash: null\n"
    )

    def test_replaces_all_three_fields(self):
        updated = build_cases.render_updated_dataset_section(
            self.SAMPLE, case_count=7, pilot_count=2, content_hash="deadbeef"
        )
        assert "cases: 7" in updated
        assert "pilot: 2" in updated
        assert 'content_hash: "deadbeef"' in updated
        assert "content_hash: null" not in updated

    def test_preserves_other_lines_untouched(self):
        updated = build_cases.render_updated_dataset_section(
            self.SAMPLE, case_count=7, pilot_count=2, content_hash="deadbeef"
        )
        assert updated.startswith("rubric_version: v1\nprompt_version: judge_v1\n")

    def test_result_is_valid_yaml_with_expected_types(self):
        updated = build_cases.render_updated_dataset_section(
            self.SAMPLE, case_count=7, pilot_count=2, content_hash="deadbeef"
        )
        parsed = yaml.safe_load(updated)
        assert parsed["dataset"]["cases"] == 7
        assert parsed["dataset"]["pilot"] == 2
        assert parsed["dataset"]["content_hash"] == "deadbeef"

    def test_numeric_looking_hash_stays_string_via_quoting(self):
        """sha256 hex가 우연히 전부 숫자여도 YAML이 정수로 오인하지 않도록 항상 인용한다."""
        updated = build_cases.render_updated_dataset_section(
            self.SAMPLE, case_count=1, pilot_count=1, content_hash="1234567890"
        )
        parsed = yaml.safe_load(updated)
        assert parsed["dataset"]["content_hash"] == "1234567890"
        assert isinstance(parsed["dataset"]["content_hash"], str)


# ---------------------------------------------------------------------------
# build_dataset — :memory: 최소 시드 통합 테스트
# ---------------------------------------------------------------------------


def _seed_minimal_db(
    conn, *, run_id: str, as_of: str, rows: list[tuple[str, str, str]], content_hash: str = "test-hash"
) -> None:
    """items + risk_results 최소 시드(rows=(item_id, grade, risk_type), 전부 동일 run)."""
    conn.executemany(
        "INSERT INTO items(item_id, item_name) VALUES (?, ?)",
        [(item_id, f"품목 {item_id}") for item_id, _, _ in rows],
    )
    conn.executemany(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade, escalated_by_notice,"
        " risk_type, score, days_to_stockout, depletion_date, factors_json)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, 50, 10, NULL, '{}')",
        [(run_id, item_id, as_of, grade, grade, risk_type) for item_id, grade, risk_type in rows],
    )
    conn.executemany(
        "INSERT INTO meta(key, value) VALUES (?, ?)",
        [("content_hash", content_hash), ("base_date", as_of)],
    )
    conn.commit()


@pytest.fixture()
def small_conn():
    conn = db.get_connection(":memory:")
    db.init_db(conn, drop=False)
    try:
        yield conn
    finally:
        conn.close()


class TestBuildDatasetIntegration:
    def _rows(self) -> list[tuple[str, str, str]]:
        return [
            ("ITM-0001", "위험", "supply_halt"),
            ("ITM-0002", "경고", "delivery_delay"),
            ("ITM-0003", "주의", "general"),
            ("ITM-0004", "주의", "general"),
            ("ITM-0005", "주의", "general"),
            ("ITM-0006", "정상", "general"),  # 제외 대상
        ]

    def test_case_count_under_target_when_insufficient_and_reports_actual(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        assert dataset["meta"]["case_count"] == 5  # 정상 등급 제외, 40 미달이어도 있는 만큼
        assert len(dataset["cases"]) == 5

    def test_case_ids_and_sorted_order(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        assert [c["case_id"] for c in dataset["cases"]] == [
            "EC-ITM-0001",
            "EC-ITM-0002",
            "EC-ITM-0003",
            "EC-ITM-0004",
            "EC-ITM-0005",
        ]

    def test_case_shape_has_required_fields(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        for case in dataset["cases"]:
            assert set(case.keys()) == {"case_id", "item_id", "run_id", "is_pilot", "evidence", "history"}
            assert case["run_id"] == RUN_ID

    def test_evidence_matches_collect_risk_evidence_directly(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        case = next(c for c in dataset["cases"] if c["item_id"] == "ITM-0001")
        expected = collect_risk_evidence(small_conn, "ITM-0001", run_id=RUN_ID)
        assert case["evidence"] == expected.model_dump()

    def test_history_uses_same_trim_rule_as_explain_item(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        for i, created_at in enumerate(
            [
                "2026-07-21T09:00:00",
                "2026-07-22T09:00:00",
                "2026-07-23T09:00:00",
                "2026-07-24T09:00:00",
            ],
            start=1,
        ):
            small_conn.execute(
                "INSERT INTO action_history(created_at, item_id, action_type, owner, note, status,"
                " risk_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, "ITM-0001", "대체 검토", "약제부", f"{i}차 조치", "완료", "supply_halt"),
            )
        small_conn.commit()

        dataset = build_cases.build_dataset(small_conn)
        case = next(c for c in dataset["cases"] if c["item_id"] == "ITM-0001")

        expected_records = queries.list_action_history(
            small_conn, item_id="ITM-0001", risk_type="supply_halt", limit=_HISTORY_LIMIT
        ).to_dict(orient="records")
        expected_history = _trim_history(expected_records)

        assert case["history"] == expected_history
        assert [h["note"] for h in case["history"]] == ["4차 조치", "3차 조치", "2차 조치"]

    def test_history_empty_when_no_action_history(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        case = next(c for c in dataset["cases"] if c["item_id"] == "ITM-0002")
        assert case["history"] == []

    def test_is_pilot_flags_match_meta_pilot_ids(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        dataset = build_cases.build_dataset(small_conn)
        pilot_ids = set(dataset["meta"]["pilot_ids"])
        assert 0 < len(pilot_ids) < len(dataset["cases"])  # 비자명한 분할(일부만 파일럿)
        for case in dataset["cases"]:
            assert case["is_pilot"] == (case["item_id"] in pilot_ids)
        assert sum(c["is_pilot"] for c in dataset["cases"]) == len(pilot_ids)

    def test_meta_fields_complete_and_correct(self, small_conn):
        _seed_minimal_db(
            small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows(), content_hash="abc123hash"
        )
        dataset = build_cases.build_dataset(small_conn)
        meta = dataset["meta"]
        assert meta["dataset_version"] == "eval_cases_v1"
        assert meta["built_from_run"] == RUN_ID
        assert meta["dataset_content_hash"] == "abc123hash"
        assert meta["params_hash"] == "a1b2c3d4"
        assert meta["case_count"] == 5
        assert isinstance(meta["pilot_ids"], list)
        assert len(meta["pilot_ids"]) == 4

    def test_determinism_two_runs_produce_identical_output(self, small_conn):
        _seed_minimal_db(small_conn, run_id=RUN_ID, as_of=AS_OF, rows=self._rows())
        first = build_cases.build_dataset(small_conn)
        second = build_cases.build_dataset(small_conn)
        assert first == second

    def test_raises_value_error_when_no_runs_exist(self, small_conn):
        with pytest.raises(ValueError):
            build_cases.build_dataset(small_conn)


# ---------------------------------------------------------------------------
# 실 커밋 산출물 대조 — eval/cases/eval_cases_v1.json ↔ eval/config.yaml
# ---------------------------------------------------------------------------


class TestRealArtifactsConsistency:
    def test_cases_file_exists_and_parses(self):
        assert CASES_PATH.exists()
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        assert "meta" in data and "cases" in data

    def test_config_content_hash_matches_cases_file_sha256(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        expected_hash = hashlib.sha256(CASES_PATH.read_bytes()).hexdigest()
        assert cfg["dataset"]["content_hash"] == expected_hash

    def test_config_case_and_pilot_counts_match_file_meta(self):
        cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        assert cfg["dataset"]["cases"] == data["meta"]["case_count"]
        assert cfg["dataset"]["pilot"] == len(data["meta"]["pilot_ids"])

    def test_cases_sorted_by_case_id_ascending(self):
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        ids = [c["case_id"] for c in data["cases"]]
        assert ids == sorted(ids)

    def test_case_count_matches_actual_list_length(self):
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        assert data["meta"]["case_count"] == len(data["cases"])

    def test_pilot_ids_are_flagged_is_pilot_in_case_list(self):
        data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        pilot_ids = set(data["meta"]["pilot_ids"])
        assert len(pilot_ids) == 4
        for case in data["cases"]:
            assert case["is_pilot"] == (case["item_id"] in pilot_ids)
