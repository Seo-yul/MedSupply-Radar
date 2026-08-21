"""공고 20건 골드 라벨(data/notices/gold/gold_labels_v1.json) 스키마·완결성 검증(Task S-24).

이 파일은 골드 라벨의 "형태"만 검증한다 — 값 자체가 원문과 맞는지의 최종 판단은 사람의 원문
판독(S-24 작업 본연)에 맡기고, 여기서는 기계로 검증 가능한 불변식만 확인한다.
  1) notices_index.csv가 정의하는 20건 notice_id 집합과 정확히 일치(과부족 없음).
  2) 각 라벨의 필드 형태가 medsupply.llm.schemas.NoticeExtraction과 호환(evidence_quotes만
     제외 — 브리프: "evidence_quotes는 골드에 불포함, 필드 정답만"; notes는 반대로 골드에만
     있는 판독 근거 부가 필드).
  3) notice_type이 ALLOWED_NOTICE_TYPES 안에 있고 notices_index.csv 표기와 일치.
  4) 날짜 필드는 ISO YYYY-MM-DD 형식(실재 달력 날짜)이거나 null.
  5) 그라운딩(날조 방지 안전망): product_names·ingredient_names·날짜 필드 값이 실제로 해당
     공고 raw txt 원문 안에 문자 그대로(부분 문자열로) 등장하는지 확인한다. "원문에 없는
     정보는 null(추정 금지)"라는 브리프 규칙을 값 단위로 기계 재확인하는 용도다 — 원문에
     정말 없는 정보인지(추정 여부)까지는 기계로 판별할 수 없으므로, 사람 판독의 대체가
     아니라 오탈자·복붙 누락 같은 실수를 잡는 안전망이다.

S-25(추출 정확도 측정)는 이 골드를 정답지로 소비하지만, 그 측정 로직 자체는 이 파일의 범위가
아니다 — tests/test_isolation.py의 GOLD_LABELS_PATH_ALLOWLIST가 그 경로(eval/·
scripts/measure_extraction.py)만 미리 문서화해 둔다.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from pathlib import Path

import pytest

from medsupply.llm.schemas import ALLOWED_NOTICE_TYPES, NoticeExtraction

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_PATH = REPO_ROOT / "data" / "notices" / "gold" / "gold_labels_v1.json"
INDEX_PATH = REPO_ROOT / "data" / "notices" / "notices_index.csv"
RAW_DIR = REPO_ROOT / "data" / "notices" / "raw"

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: NoticeExtraction에는 있지만 골드에는 없는 필드(브리프: "evidence_quotes는 골드에 불포함").
_EXTRACTION_ONLY_FIELDS = {"evidence_quotes"}
#: 골드에는 있지만 NoticeExtraction에는 없는 필드(판독 근거 — 브리프: "notes: 판독 근거 한 줄").
_GOLD_ONLY_FIELDS = {"notes"}


def _load_gold() -> dict:
    return json.loads(GOLD_PATH.read_text(encoding="utf-8"))


def _labels() -> dict[str, dict]:
    return _load_gold()["labels"]


def _index_rows() -> list[dict[str, str]]:
    with INDEX_PATH.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _index_notice_ids() -> set[str]:
    return {f"N-{row['file'][:3]}" for row in _index_rows()}


def _index_notice_type_by_id() -> dict[str, str]:
    return {f"N-{row['file'][:3]}": row["notice_type"] for row in _index_rows()}


def _raw_text_for(notice_id: str) -> str:
    """notice_id(예: 'N-009')에 대응하는 raw txt 파일 전체 텍스트(헤더 3줄 포함)를 반환한다.

    scripts/load_notices.py의 _notice_id_from_filename과 반대 방향 매핑("N-009" -> "009_"
    접두 파일)이며, 파일명 규칙(notices_index.csv의 file 컬럼)에 의존한다.
    """
    num = notice_id.removeprefix("N-")
    matches = list(RAW_DIR.glob(f"{num}_*.txt"))
    assert len(matches) == 1, f"{notice_id}: raw 파일이 정확히 1개가 아님({matches})"
    return matches[0].read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 파일 존재·최상위 구조
# ---------------------------------------------------------------------------


class TestGoldFileStructure:
    def test_gold_file_exists_and_parses(self):
        assert GOLD_PATH.exists()
        assert isinstance(_load_gold(), dict)

    def test_version_tag(self):
        assert _load_gold()["version"] == "v1"

    def test_top_level_has_only_version_and_labels(self):
        assert set(_load_gold().keys()) == {"version", "labels"}


# ---------------------------------------------------------------------------
# notice_id 완결성 — notices_index.csv(20건)와 정확히 일치
# ---------------------------------------------------------------------------


class TestNoticeIdCompleteness:
    def test_exactly_20_labels(self):
        assert len(_labels()) == 20

    def test_notice_id_set_matches_index(self):
        assert set(_labels().keys()) == _index_notice_ids()

    def test_notice_id_format(self):
        for notice_id in _labels():
            assert re.fullmatch(r"N-\d{3}", notice_id), notice_id


# ---------------------------------------------------------------------------
# 필드 형태 — NoticeExtraction과 호환(evidence_quotes 제외 + notes 추가)
# ---------------------------------------------------------------------------


class TestFieldShapes:
    def test_label_keys_match_notice_extraction_fields_plus_notes(self):
        expected = (set(NoticeExtraction.model_fields) - _EXTRACTION_ONLY_FIELDS) | _GOLD_ONLY_FIELDS
        for notice_id, entry in _labels().items():
            assert set(entry.keys()) == expected, notice_id

    def test_product_names_is_nonempty_string_list(self):
        for notice_id, entry in _labels().items():
            names = entry["product_names"]
            assert isinstance(names, list) and names, notice_id
            assert all(isinstance(n, str) and n.strip() for n in names), notice_id

    def test_ingredient_names_is_nonempty_string_list(self):
        for notice_id, entry in _labels().items():
            names = entry["ingredient_names"]
            assert isinstance(names, list) and names, notice_id
            assert all(isinstance(n, str) and n.strip() for n in names), notice_id

    def test_reason_is_nonempty_string(self):
        for notice_id, entry in _labels().items():
            assert isinstance(entry["reason"], str) and entry["reason"].strip(), notice_id

    def test_notice_type_is_allowed_value(self):
        for notice_id, entry in _labels().items():
            assert entry["notice_type"] in ALLOWED_NOTICE_TYPES, notice_id

    def test_notice_type_matches_notices_index(self):
        """골드 notice_type은 원문 자체 표기(보고구분)이므로 notices_index.csv와도 같아야
        한다 — 다르면 독립 판독이 색인과 다른 유형으로 읽었다는 뜻이라 재확인이 필요하다."""
        index_type_by_id = _index_notice_type_by_id()
        for notice_id, entry in _labels().items():
            assert entry["notice_type"] == index_type_by_id[notice_id], notice_id

    def test_notes_is_string_or_null(self):
        for notice_id, entry in _labels().items():
            notes = entry["notes"]
            assert notes is None or (isinstance(notes, str) and notes.strip()), notice_id


# ---------------------------------------------------------------------------
# 날짜 필드 — ISO YYYY-MM-DD(실재 달력 날짜) 또는 null(브리프: "원문에 없으면 null")
# ---------------------------------------------------------------------------


class TestDateFields:
    @pytest.mark.parametrize("field", ["halt_start_date", "expected_restart_date"])
    def test_date_fields_are_iso_format_or_null(self, field):
        for notice_id, entry in _labels().items():
            value = entry[field]
            if value is None:
                continue
            assert _ISO_DATE_RE.fullmatch(value), f"{notice_id}.{field}={value!r}"
            date.fromisoformat(value)  # 실재 달력 날짜인지(예: 2월 30일 같은 값 방지)

    def test_halt_start_date_never_null(self):
        """이 20건은 전부 원문에 공급중단일자 또는 공급부족발생 예상일자 중 하나가 명시돼
        있어 halt_start_date가 null인 건이 없다 — 이 데이터셋에 대한 실측 사실이며, null이
        나오면 회귀(정답 파일이 손상되었거나 다른 데이터셋으로 바뀌었을 가능성)로 본다."""
        for notice_id, entry in _labels().items():
            assert entry["halt_start_date"] is not None, notice_id


# ---------------------------------------------------------------------------
# NoticeExtraction 파싱 호환성 — evidence_quotes만 채워 넣으면 그대로 유효 모델이어야 함
# ---------------------------------------------------------------------------


class TestNoticeExtractionCompatibility:
    def test_label_plus_empty_evidence_parses_as_notice_extraction(self):
        for notice_id, entry in _labels().items():
            payload = {k: v for k, v in entry.items() if k not in _GOLD_ONLY_FIELDS}
            payload["evidence_quotes"] = []
            NoticeExtraction.model_validate(payload)  # 예외 없이 파싱되면 통과


# ---------------------------------------------------------------------------
# 그라운딩(날조 방지 안전망) — 값이 실제로 해당 공고 원문 안에 문자 그대로 등장하는지
# ---------------------------------------------------------------------------


class TestGroundedInRawText:
    def test_product_names_are_grounded_in_raw_text(self):
        for notice_id, entry in _labels().items():
            raw = _raw_text_for(notice_id)
            for name in entry["product_names"]:
                assert name in raw, f"{notice_id}: product_name {name!r} not found in raw text"

    def test_ingredient_names_are_grounded_in_raw_text(self):
        for notice_id, entry in _labels().items():
            raw = _raw_text_for(notice_id)
            for name in entry["ingredient_names"]:
                assert name in raw, f"{notice_id}: ingredient_name {name!r} not found in raw text"

    @pytest.mark.parametrize("field", ["halt_start_date", "expected_restart_date"])
    def test_date_fields_are_grounded_in_raw_text(self, field):
        for notice_id, entry in _labels().items():
            value = entry[field]
            if value is None:
                continue
            raw = _raw_text_for(notice_id)
            assert value in raw, f"{notice_id}.{field}={value!r} not found in raw text"
