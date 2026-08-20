"""Task M-14: medsupply/llm/mapping.py(결정적 품목 매핑) + process_notice 파이프라인 테스트.

매핑(map_extraction_to_items)은 LLM이 전혀 관여하지 않는 결정적 조인이므로, 실제 LLM
호출 없이 tmp in-memory DB(:memory:)에 소형 기준정보(ingredients/ingredient_aliases/
substitute_groups/items)를 직접 INSERT해 규칙 하나하나를 검증한다.

process_notice·CLI(scripts/process_notices.py) 테스트는 medsupply.llm.mapping
네임스페이스로 들여온 extract_notice를 모킹해 LLM 호출 자체를 완전히 배제한다
(tests/llm/test_extraction.py의 complete_json 모킹과 동일한 원칙 — "from ... import"로
들여온 이름은 정의 모듈이 아니라 사용하는 모듈(mapping) 쪽을 patch해야 한다).
"""

from __future__ import annotations

import sqlite3

import pytest

from medsupply.llm import mapping as mapping_module
from medsupply.llm.extraction import ExtractionResult
from medsupply.llm.mapping import map_extraction_to_items, process_notice
from medsupply.llm.schemas import NoticeExtraction
from scripts import process_notices

# ---------------------------------------------------------------------------
# 소형 기준정보 — ingredients / ingredient_aliases / substitute_groups / items
# ---------------------------------------------------------------------------

ING_CFT = "ING-CFT-001"  # kr="세프트리악손", en="Ceftriaxone", 별칭(salt)="세프트리악손나트륨"
ING_VAN = "ING-VAN-001"  # kr="반코마이신염산염" (별칭 없음 — 부분매칭 경로 전용)

SG_CFT_1 = "SG-CFT-1"
SG_CFT_2 = "SG-CFT-2"
SG_VAN_1 = "SG-VAN-1"

ITEM_CFT_A = "ITEM-CFT-A"
ITEM_CFT_B = "ITEM-CFT-B"
ITEM_CFT_C = "ITEM-CFT-C"
ITEM_VAN_A = "ITEM-VAN-A"
ITEM_ETC_A = "ITEM-ETC-A"  # 성분 미등록 — 제품명 매칭 보조 경로 전용

NOTICE_ID = "N-TEST-001"


@pytest.fixture()
def conn(empty_conn: sqlite3.Connection) -> sqlite3.Connection:
    """empty_conn(스키마만 적용된 :memory:) 위에 매핑 테스트 전용 소형 기준정보를 얹는다."""
    empty_conn.executemany(
        "INSERT INTO ingredients(ingredient_code, ingredient_name_kr, ingredient_name_en, atc_code)"
        " VALUES (?, ?, ?, ?)",
        [
            (ING_CFT, "세프트리악손", "Ceftriaxone", "J01DD04"),
            (ING_VAN, "반코마이신염산염", "Vancomycin Hydrochloride", "J01XA01"),
        ],
    )
    empty_conn.execute(
        "INSERT INTO ingredient_aliases(alias, ingredient_code, alias_type) VALUES (?, ?, ?)",
        ("세프트리악손나트륨", ING_CFT, "salt"),
    )
    empty_conn.executemany(
        "INSERT INTO substitute_groups(substitute_group_id, ingredient_code, strength, form,"
        " route, group_label) VALUES (?, ?, ?, ?, ?, ?)",
        [
            (SG_CFT_1, ING_CFT, "1g", "주사제", "정맥", "세프트리악손 1g 주사"),
            (SG_CFT_2, ING_CFT, "500mg", "주사제", "정맥", "세프트리악손 500mg 주사"),
            (SG_VAN_1, ING_VAN, "500mg", "주사제", "정맥", "반코마이신 500mg 주사"),
        ],
    )
    # ITEM_CFT_B를 A보다 먼저 넣어 "mapped가 item_id 오름차순"이 삽입 순서가 아니라
    # 정렬 로직 자체에서 나오는지 검증한다.
    empty_conn.executemany(
        "INSERT INTO items(item_id, item_name, standard_code, ingredient_code, strength, form,"
        " route, pack_size, supplier, is_essential, substitute_group_id, atc_code)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                ITEM_CFT_B, "세프트리악손주 1g(대한제약)", "8806000000002", ING_CFT, "1g",
                "주사제", "정맥", 10, "대한제약", 0, SG_CFT_1, "J01DD04",
            ),
            (
                ITEM_CFT_A, "세프트리악손주 1g(한국제약)", "8806000000001", ING_CFT, "1g",
                "주사제", "정맥", 10, "한국제약", 1, SG_CFT_1, "J01DD04",
            ),
            (
                ITEM_CFT_C, "세프트리악손주 500mg", "8806000000003", ING_CFT, "500mg",
                "주사제", "정맥", 10, "한국제약", 0, SG_CFT_2, "J01DD04",
            ),
            (
                ITEM_VAN_A, "반코마이신주 500mg", "8806000000004", ING_VAN, "500mg",
                "주사제", "정맥", 10, "한국제약", 0, SG_VAN_1, "J01XA01",
            ),
            (
                ITEM_ETC_A, "특수의약품정 10mg", "8806000000005", None, "10mg",
                "정제", "경구", 30, "기타제약", 0, None, None,
            ),
        ],
    )
    empty_conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
        " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            NOTICE_ID, "2026-08-01", "세프트리악손주 공급중단 안내", "테스트출처",
            "https://example.invalid/notice/test", "제조소 사정으로 공급이 중단됩니다.",
            "공급중단", "2026-08-01T09:00:00",
        ),
    )
    empty_conn.commit()
    return empty_conn


def _extraction(**overrides) -> NoticeExtraction:
    defaults = dict(
        product_names=[],
        ingredient_names=[],
        reason="제조소 사정",
        halt_start_date=None,
        expected_restart_date=None,
        notice_type="공급중단",
        evidence_quotes=[],
    )
    defaults.update(overrides)
    return NoticeExtraction(**defaults)


def _extraction_result(**overrides) -> ExtractionResult:
    defaults = dict(
        extraction=_extraction(ingredient_names=["세프트리악손"]),
        confidence=0.95,
        status="자동확정",
        verification={
            "quotes_total": 0, "quotes_found": 0, "missing_fields": [],
            "date_parse_ok": True, "notes": [],
        },
        provider="anthropic",
        model="claude-opus-5",
        prompt_version="v1",
        cache_hit=False,
    )
    defaults.update(overrides)
    return ExtractionResult(**defaults)


# ---------------------------------------------------------------------------
# 규칙 2a/2b — 성분명(kr/en)·별칭 정규화 완전 일치
# ---------------------------------------------------------------------------


class TestExactIngredientMatch:
    def test_kr_name_exact_match_maps_all_items_of_ingredient(self, conn):
        extraction = _extraction(ingredient_names=["세프트리악손"])

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == (ING_CFT,)
        assert [m["item_id"] for m in result.mapped] == [ITEM_CFT_A, ITEM_CFT_B, ITEM_CFT_C]
        assert all(m["match_basis"] == "ingredient" for m in result.mapped)
        assert all(m["needs_review"] == 0 for m in result.mapped)
        assert {m["substitute_group_id"] for m in result.mapped} == {SG_CFT_1, SG_CFT_2}
        assert result.unmatched_ingredients == ()
        assert result.unmatched_products == ()

    def test_en_name_exact_match_is_casefold_insensitive(self, conn):
        extraction = _extraction(ingredient_names=["CEFTRIAXONE"])  # DB: "Ceftriaxone"

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == (ING_CFT,)
        assert [m["item_id"] for m in result.mapped] == [ITEM_CFT_A, ITEM_CFT_B, ITEM_CFT_C]
        assert all(m["match_basis"] == "ingredient" for m in result.mapped)
        assert result.unmatched_ingredients == ()

    def test_salt_alias_exact_match(self, conn):
        """별칭 사전의 salt형 표기("세프트리악손나트륨")가 정규화 완전 일치로 매칭된다."""
        extraction = _extraction(ingredient_names=["세프트리악손나트륨"])

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == (ING_CFT,)
        assert [m["item_id"] for m in result.mapped] == [ITEM_CFT_A, ITEM_CFT_B, ITEM_CFT_C]
        assert all(m["match_basis"] == "ingredient" for m in result.mapped)
        assert all(m["needs_review"] == 0 for m in result.mapped)


# ---------------------------------------------------------------------------
# 규칙 2c — 부분 포함 매칭(길이 ≥4)
# ---------------------------------------------------------------------------


class TestPartialIngredientMatch:
    def test_extracted_value_containing_full_ingredient_name_matches_partially(self, conn):
        """LLM이 수화물 등 접미를 붙여 추출해(정확 일치 실패) 부분 포함으로만 걸리는 경로."""
        extraction = _extraction(ingredient_names=["반코마이신염산염수화물"])

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == (ING_VAN,)
        assert [m["item_id"] for m in result.mapped] == [ITEM_VAN_A]
        assert result.mapped[0]["match_basis"] == "ingredient_partial"
        assert result.mapped[0]["needs_review"] == 1
        assert result.unmatched_ingredients == ()

    def test_short_substring_below_min_length_does_not_match(self, conn):
        """길이 <4 문자열은 포함 관계여도 매칭하지 않는다(규칙 2c 길이 조건)."""
        extraction = _extraction(ingredient_names=["반코"])  # "반코마이신염산염"에 포함되지만 len=2

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == ()
        assert result.mapped == ()
        assert result.unmatched_ingredients == ("반코",)


# ---------------------------------------------------------------------------
# 규칙 5 — needs_review: extraction_status가 '확인 필요'면 전 행 강제 1
# ---------------------------------------------------------------------------


class TestNeedsReviewForcedByExtractionStatus:
    def test_review_required_status_forces_needs_review_on_every_row_even_exact_basis(self, conn):
        extraction = _extraction(ingredient_names=["세프트리악손"])  # 기본은 needs_review 0인 정확 매칭

        result = map_extraction_to_items(conn, extraction, extraction_status="확인 필요")

        assert len(result.mapped) == 3
        assert all(m["needs_review"] == 1 for m in result.mapped)
        assert all(m["match_basis"] == "ingredient" for m in result.mapped)  # basis 자체는 불변


# ---------------------------------------------------------------------------
# 규칙 3/6 — 제품명 보조 경로 · 미매칭
# ---------------------------------------------------------------------------


class TestProductNameFallbackAndUnmatched:
    def test_product_name_exact_match_used_only_when_ingredient_matching_fails(self, conn):
        extraction = _extraction(
            ingredient_names=["이세상에없는성분명123"],
            product_names=["특수의약품정 10mg"],
        )

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == ()
        assert [m["item_id"] for m in result.mapped] == [ITEM_ETC_A]
        assert result.mapped[0]["match_basis"] == "product"
        assert result.mapped[0]["needs_review"] == 0
        assert result.unmatched_ingredients == ("이세상에없는성분명123",)
        assert result.unmatched_products == ()

    def test_product_names_are_not_evaluated_when_ingredient_matching_succeeds(self, conn):
        """성분 매칭이 성공하면 제품명은 시도조차 하지 않는다(2순위·보조 규정)."""
        extraction = _extraction(
            ingredient_names=["세프트리악손"],
            product_names=["이세상에없는제품명456"],
        )

        result = map_extraction_to_items(conn, extraction)

        assert result.matched_ingredient_codes == (ING_CFT,)
        assert result.unmatched_products == ()

    def test_nothing_matches_returns_empty_mapped_with_both_unmatched_recorded(self, conn):
        extraction = _extraction(
            ingredient_names=["이세상에없는성분명123"],
            product_names=["이세상에없는제품명456"],
        )

        result = map_extraction_to_items(conn, extraction)

        assert result.mapped == ()
        assert result.matched_ingredient_codes == ()
        assert result.unmatched_ingredients == ("이세상에없는성분명123",)
        assert result.unmatched_products == ("이세상에없는제품명456",)

    def test_no_extraction_names_at_all_is_not_an_error(self, conn):
        extraction = _extraction(ingredient_names=[], product_names=[])

        result = map_extraction_to_items(conn, extraction)

        assert result.mapped == ()
        assert result.matched_ingredient_codes == ()
        assert result.unmatched_ingredients == ()
        assert result.unmatched_products == ()


# ---------------------------------------------------------------------------
# 결정성
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_same_input_called_twice_yields_identical_result_including_mapped_order(self, conn):
        extraction = _extraction(ingredient_names=["세프트리악손나트륨"])

        result1 = map_extraction_to_items(conn, extraction)
        result2 = map_extraction_to_items(conn, extraction)

        assert result1 == result2
        assert result1.mapped == result2.mapped


# ---------------------------------------------------------------------------
# process_notice — extract_notice 모킹, writer 실호출(멱등성은 실제 DB로 검증)
# ---------------------------------------------------------------------------


class TestProcessNotice:
    def test_unknown_notice_id_raises_value_error(self, conn, monkeypatch):
        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("raw_text 조회가 실패했으므로 extract_notice가 호출되면 안 된다")

        monkeypatch.setattr(mapping_module, "extract_notice", _must_not_be_called)

        with pytest.raises(ValueError):
            process_notice(conn, "NO-SUCH-NOTICE")

    def test_writer_receives_dict_payload_and_list_mapped_with_expected_fields(self, conn, monkeypatch):
        fake_result = _extraction_result()
        monkeypatch.setattr(mapping_module, "extract_notice", lambda raw_text, **kw: fake_result)

        captured: dict = {}

        def fake_save(conn_arg, **kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(mapping_module.writer, "save_notice_extraction", fake_save)

        result = process_notice(conn, NOTICE_ID)

        assert isinstance(captured["payload"], dict)
        assert captured["payload"] == fake_result.extraction.model_dump()
        assert isinstance(captured["mapped"], list)
        assert len(captured["mapped"]) == 3
        assert all(isinstance(m, dict) for m in captured["mapped"])
        assert captured["notice_id"] == NOTICE_ID
        assert captured["confidence"] == 0.95
        assert captured["status"] == "자동확정"
        assert captured["prompt_version"] == "v1"
        assert captured["provider"] == "anthropic"
        assert captured["model"] == "claude-opus-5"

        assert result.notice_id == NOTICE_ID
        assert result.status == "자동확정"
        assert result.confidence == 0.95
        assert result.mapped_count == 3
        assert result.matched_ingredients == 1
        assert result.cache_hit is False

    def test_force_refresh_and_notice_id_are_passed_through_to_extract_notice(self, conn, monkeypatch):
        fake_result = _extraction_result()
        captured_kwargs: dict = {}

        def fake_extract(raw_text, **kwargs):
            captured_kwargs.update(kwargs)
            return fake_result

        monkeypatch.setattr(mapping_module, "extract_notice", fake_extract)

        process_notice(conn, NOTICE_ID, force_refresh=True)

        assert captured_kwargs["force_refresh"] is True
        assert captured_kwargs["notice_id"] == NOTICE_ID

    def test_reprocessing_is_idempotent_via_writer_replace_semantics(self, conn, monkeypatch):
        """writer의 기존 규칙(INSERT OR REPLACE + notice_item_map 교체)이 process_notice를
        거쳐도 그대로 유지되는지 실제 DB로 검증한다(재처리해도 행 수가 늘지 않는다)."""
        fake_result = _extraction_result()
        monkeypatch.setattr(mapping_module, "extract_notice", lambda raw_text, **kw: fake_result)

        process_notice(conn, NOTICE_ID)
        process_notice(conn, NOTICE_ID)

        extraction_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM notice_extractions WHERE notice_id = ?", (NOTICE_ID,)
        ).fetchone()["c"]
        assert extraction_rows == 1

        mapped_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM notice_item_map WHERE notice_id = ?", (NOTICE_ID,)
        ).fetchone()["c"]
        assert mapped_rows == 3


# ---------------------------------------------------------------------------
# CLI(scripts/process_notices.py) — --all 모킹 경로 스모크
# ---------------------------------------------------------------------------


class TestCli:
    def _seed_notices_only_db(self, db_path) -> None:
        from medsupply.data import db as db_module

        connection = db_module.get_connection(str(db_path))
        db_module.init_db(connection, drop=False)
        connection.executemany(
            "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
            " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("N-001", "2026-08-01", "공고1", "출처", None, "원문1", "공급중단", "2026-08-01T09:00:00"),
                ("N-002", "2026-08-02", "공고2", "출처", None, "원문2", "공급중단", "2026-08-02T09:00:00"),
                ("N-003", "2026-08-03", "공고3", "출처", None, "원문3", "공급중단", "2026-08-03T09:00:00"),
            ],
        )
        connection.commit()
        connection.close()

    def test_all_continues_past_failure_and_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "cli_all_test.db"
        self._seed_notices_only_db(db_path)

        def fake_extract_notice(raw_text, *, notice_id=None, **kwargs):
            if notice_id == "N-002":
                raise RuntimeError("LLM 호출 실패 시뮬레이션")
            return _extraction_result(extraction=_extraction())  # ingredient_names=[] → mapped=[]

        monkeypatch.setattr(mapping_module, "extract_notice", fake_extract_notice)

        exit_code = process_notices.main(["--db", str(db_path), "--all"])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "N-001" in captured.out
        assert "N-002" in captured.out
        assert "N-003" in captured.out  # 실패 이후에도 계속 진행됐다
        assert "실패 건수: 1" in captured.out
        assert "자동확정 건수: 2" in captured.out

    def test_notice_id_flag_processes_single_notice_and_exits_zero(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "cli_single_test.db"
        self._seed_notices_only_db(db_path)

        monkeypatch.setattr(
            mapping_module, "extract_notice",
            lambda raw_text, **kw: _extraction_result(extraction=_extraction()),
        )

        exit_code = process_notices.main(["--db", str(db_path), "--notice-id", "N-001"])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "N-001" in captured.out

    def test_missing_target_flag_exits_nonzero_via_argparse(self, tmp_path):
        db_path = tmp_path / "cli_no_target.db"
        self._seed_notices_only_db(db_path)

        with pytest.raises(SystemExit) as exc_info:
            process_notices.main(["--db", str(db_path)])

        assert exc_info.value.code == 2
