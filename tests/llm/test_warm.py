"""Task M-27: LLM 캐시 선워밍(medsupply/llm/warm.py) + CLI(scripts/warm_cache.py) 테스트.

process_notice(medsupply.llm.mapping)·explain_item(medsupply.llm.explanation)은 이미
M-14·M-21에서 검증이 끝난 완성 함수라 여기서는 재검증하지 않는다 — warm 네임스페이스로
들여온 두 이름을 monkeypatch해(tests/llm/test_mapping.py의 "from ... import한 이름은
사용하는 모듈을 patch" 원칙과 동일) LLM 호출 자체를 배제하고, warm_cache()가 담당하는
"대상 선정 · 순서 · 실패 격리 · cache_hit 집계"만 검증한다.

대상 선정은 실제 :memory: DB로 검증한다 — scope='notices'는 notices 전 건, scope=
'explanations'는 최신 run에서 grade ∈ {위험,경고,주의}(정상 제외) 품목만 item_id
오름차순으로.

모킹 반환값은 warm.py가 실제로 읽는 유일한 필드(cache_hit)만 갖춘 types.SimpleNamespace를
쓴다 — NoticeProcessingResult·ExplanationResult 풀 스펙(특히 RiskExplanation 중첩 구성)을
갖출 이유가 없다(warm.py는 그 필드들을 전혀 읽지 않는다).
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from medsupply.llm import warm as warm_module
from medsupply.llm.warm import warm_cache
from scripts import warm_cache as warm_cache_script

# ---------------------------------------------------------------------------
# 소형 기준정보 — items / notices / risk_results(최신 run 1개 + 구 run 1개)
# ---------------------------------------------------------------------------

ITEM_DANGER = "ITEM-DANGER"  # 최신 run grade=위험
ITEM_WARN = "ITEM-WARN"  # 최신 run grade=경고
ITEM_CAUTION = "ITEM-CAUTION"  # 최신 run grade=주의
ITEM_NORMAL = "ITEM-NORMAL"  # 최신 run grade=정상 — 설명 대상에서 제외돼야 함

NOTICE_1 = "N-0001"
NOTICE_2 = "N-0002"
NOTICE_3 = "N-0003"

RUN_LATEST = "2026-08-01#aaaaaaaa"
RUN_OLD = "2026-07-31#aaaaaaaa"


def _result(*, cache_hit: bool = False) -> SimpleNamespace:
    """warm.py가 읽는 유일한 필드(cache_hit)만 갖춘 최소 페이크 결과."""
    return SimpleNamespace(cache_hit=cache_hit)


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO items(item_id, item_name) VALUES (?, ?)",
        [
            (ITEM_DANGER, "위험 품목"),
            (ITEM_WARN, "경고 품목"),
            (ITEM_CAUTION, "주의 품목"),
            (ITEM_NORMAL, "정상 품목"),
        ],
    )
    conn.executemany(
        "INSERT INTO notices(notice_id, published_date, title, notice_type)"
        " VALUES (?, ?, ?, ?)",
        [
            (NOTICE_1, "2026-07-28", "공고1", "공급중단"),
            (NOTICE_2, "2026-07-29", "공고2", "공급부족"),
            (NOTICE_3, "2026-07-30", "공고3", "기타"),
        ],
    )
    conn.executemany(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            # 구 run(다른 as_of, 같은 패밀리) — get_latest_runs가 이 행을 제외해야 함을
            # 함께 검증(ITEM_DANGER의 최신 등급은 위험이지만 구 run에서는 주의였다).
            (RUN_OLD, ITEM_DANGER, "2026-07-31", "주의", "주의"),
            (RUN_LATEST, ITEM_DANGER, "2026-08-01", "위험", "경고"),
            (RUN_LATEST, ITEM_WARN, "2026-08-01", "경고", "경고"),
            (RUN_LATEST, ITEM_CAUTION, "2026-08-01", "주의", "주의"),
            (RUN_LATEST, ITEM_NORMAL, "2026-08-01", "정상", "정상"),
        ],
    )
    conn.commit()


@pytest.fixture()
def conn(empty_conn: sqlite3.Connection) -> sqlite3.Connection:
    """empty_conn(스키마만 적용된 :memory:) 위에 warm_cache 대상 선정 전용 데이터를 얹는다."""
    _seed(empty_conn)
    return empty_conn


# ---------------------------------------------------------------------------
# scope='notices' — 전 공고, 등급과 무관
# ---------------------------------------------------------------------------


class TestScopeNotices:
    def test_processes_every_notice_and_never_touches_explanations(self, conn, monkeypatch):
        called: list[str] = []

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            called.append(notice_id)
            return _result()

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("scope='notices'는 explain_item을 호출하면 안 된다")

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", _must_not_be_called)

        report = warm_cache(conn, scope="notices")

        assert sorted(called) == [NOTICE_1, NOTICE_2, NOTICE_3]
        assert report.notices_total == 3
        assert report.notices_ok == 3
        assert report.notices_failed == ()
        assert report.explanations_total == 0
        assert report.explanations_ok == 0
        assert report.explanations_failed == ()

    def test_two_failures_are_isolated_and_recorded(self, conn, monkeypatch):
        called: list[str] = []

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            called.append(notice_id)
            if notice_id in (NOTICE_1, NOTICE_3):
                raise RuntimeError(f"LLM 호출 실패: {notice_id}")
            return _result()

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)

        report = warm_cache(conn, scope="notices")

        assert sorted(called) == [NOTICE_1, NOTICE_2, NOTICE_3]  # 실패 이후에도 계속 진행
        assert report.notices_total == 3
        assert report.notices_ok == 1
        assert set(report.notices_failed) == {NOTICE_1, NOTICE_3}
        assert len(report.notices_failed) == 2


# ---------------------------------------------------------------------------
# scope='explanations' — 최신 run에서 정상 제외, item_id 오름차순
# ---------------------------------------------------------------------------


class TestScopeExplanations:
    def test_targets_only_non_normal_grades_at_latest_run_sorted_by_item_id(
        self, conn, monkeypatch
    ):
        called: list[str] = []

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            called.append(item_id)
            return _result()

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("scope='explanations'는 process_notice를 호출하면 안 된다")

        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)
        monkeypatch.setattr(warm_module, "process_notice", _must_not_be_called)

        report = warm_cache(conn, scope="explanations")

        assert called == [ITEM_CAUTION, ITEM_DANGER, ITEM_WARN]  # item_id asc, 정상 제외
        assert report.explanations_total == 3
        assert report.explanations_ok == 3
        assert report.explanations_failed == ()
        assert report.notices_total == 0
        assert report.notices_ok == 0
        assert report.notices_failed == ()

    def test_two_failures_are_isolated_and_recorded(self, conn, monkeypatch):
        called: list[str] = []

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            called.append(item_id)
            if item_id in (ITEM_DANGER, ITEM_WARN):
                raise RuntimeError(f"LLM 호출 실패: {item_id}")
            return _result()

        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        report = warm_cache(conn, scope="explanations")

        assert called == [ITEM_CAUTION, ITEM_DANGER, ITEM_WARN]
        assert report.explanations_total == 3
        assert report.explanations_ok == 1
        assert set(report.explanations_failed) == {ITEM_DANGER, ITEM_WARN}
        assert len(report.explanations_failed) == 2

    def test_no_runs_at_all_yields_empty_target_list_without_error(
        self, empty_conn, monkeypatch
    ):
        """risk_results가 아예 비어 있으면(최신 run 없음) 대상 없음 — 에러가 아니다."""

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("대상이 없으므로 explain_item이 호출되면 안 된다")

        monkeypatch.setattr(warm_module, "explain_item", _must_not_be_called)

        report = warm_cache(empty_conn, scope="explanations")

        assert report.explanations_total == 0
        assert report.explanations_ok == 0
        assert report.explanations_failed == ()


# ---------------------------------------------------------------------------
# scope='all' — 공고 먼저 → 설명(순서 고정) + 결정성
# ---------------------------------------------------------------------------


class TestScopeAll:
    def test_notices_are_fully_processed_before_any_explanation_call(self, conn, monkeypatch):
        call_order: list[str] = []

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            call_order.append(f"notice:{notice_id}")
            return _result()

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            call_order.append(f"explain:{item_id}")
            return _result()

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        report = warm_cache(conn, scope="all")

        assert len(call_order) == 6  # 공고 3 + 설명 3
        last_notice_index = max(
            i for i, entry in enumerate(call_order) if entry.startswith("notice:")
        )
        first_explain_index = min(
            i for i, entry in enumerate(call_order) if entry.startswith("explain:")
        )
        assert last_notice_index < first_explain_index

        assert report.notices_total == 3
        assert report.notices_ok == 3
        assert report.explanations_total == 3
        assert report.explanations_ok == 3

    def test_target_lists_and_order_are_deterministic_across_two_calls(self, conn, monkeypatch):
        call_log: list[str] = []

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            call_log.append(f"notice:{notice_id}")
            return _result()

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            call_log.append(f"explain:{item_id}")
            return _result()

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        warm_cache(conn, scope="all")
        first_run = list(call_log)
        call_log.clear()

        warm_cache(conn, scope="all")
        second_run = list(call_log)

        assert first_run == second_run
        assert len(first_run) == 6


# ---------------------------------------------------------------------------
# cache_hit 집계
# ---------------------------------------------------------------------------


class TestCacheHitAggregation:
    def test_cache_hits_sum_successful_results_across_both_scopes(self, conn, monkeypatch):
        notice_cache_hits = {NOTICE_1: True, NOTICE_2: True, NOTICE_3: False}
        explanation_cache_hits = {ITEM_DANGER: True, ITEM_WARN: False, ITEM_CAUTION: False}

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            return _result(cache_hit=notice_cache_hits[notice_id])

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            return _result(cache_hit=explanation_cache_hits[item_id])

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        report = warm_cache(conn, scope="all")

        assert report.cache_hits == 3  # 공고 2건 + 설명 1건

    def test_failed_targets_do_not_count_as_cache_hits(self, conn, monkeypatch):
        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            raise RuntimeError("실패")

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)

        report = warm_cache(conn, scope="notices")

        assert report.cache_hits == 0


# ---------------------------------------------------------------------------
# force_refresh 전파
# ---------------------------------------------------------------------------


class TestForceRefreshPropagation:
    def test_force_refresh_flag_is_forwarded_to_both_process_notice_and_explain_item(
        self, conn, monkeypatch
    ):
        captured: dict[str, list[bool]] = {"notice": [], "explain": []}

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            captured["notice"].append(force_refresh)
            return _result()

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            captured["explain"].append(force_refresh)
            return _result()

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        warm_cache(conn, scope="all", force_refresh=True)
        assert captured["notice"] == [True, True, True]
        assert captured["explain"] == [True, True, True]

        captured["notice"].clear()
        captured["explain"].clear()

        warm_cache(conn, scope="all", force_refresh=False)
        assert captured["notice"] == [False, False, False]
        assert captured["explain"] == [False, False, False]


# ---------------------------------------------------------------------------
# progress 콜백 — CLI 출력용, None이면 무시
# ---------------------------------------------------------------------------


class TestProgressCallback:
    def test_progress_is_invoked_once_per_target_and_ignored_when_none(self, conn, monkeypatch):
        monkeypatch.setattr(
            warm_module,
            "process_notice",
            lambda conn_arg, notice_id, *, force_refresh=False: _result(),
        )
        monkeypatch.setattr(
            warm_module,
            "explain_item",
            lambda conn_arg, item_id, *, force_refresh=False: _result(),
        )

        messages: list[str] = []
        warm_cache(conn, scope="all", progress=messages.append)
        assert len(messages) == 6  # 공고 3 + 설명 3

        # progress=None(기본)이어도 예외 없이 완료된다.
        warm_cache(conn, scope="all")


# ---------------------------------------------------------------------------
# scope 검증
# ---------------------------------------------------------------------------


class TestInvalidScope:
    def test_unknown_scope_raises_value_error(self, conn):
        with pytest.raises(ValueError):
            warm_cache(conn, scope="unknown")


# ---------------------------------------------------------------------------
# CLI(scripts/warm_cache.py) — 모킹 경로 스모크
# ---------------------------------------------------------------------------


class TestCli:
    def _seed_db(self, db_path) -> None:
        from medsupply.data import db as db_module

        connection = db_module.get_connection(str(db_path))
        db_module.init_db(connection, drop=False)
        _seed(connection)
        connection.close()

    def test_all_success_exits_zero_and_prints_summary(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "cli_success.db"
        self._seed_db(db_path)

        monkeypatch.setattr(
            warm_module,
            "process_notice",
            lambda conn_arg, notice_id, *, force_refresh=False: _result(cache_hit=True),
        )
        monkeypatch.setattr(
            warm_module,
            "explain_item",
            lambda conn_arg, item_id, *, force_refresh=False: _result(cache_hit=False),
        )

        exit_code = warm_cache_script.main(["--db", str(db_path)])

        captured = capsys.readouterr()
        assert exit_code == 0
        assert "실패 0건" in captured.out
        assert "캐시 적중: 3건" in captured.out

    def test_some_failures_exit_nonzero_and_list_failed_ids(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "cli_partial_fail.db"
        self._seed_db(db_path)

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            if notice_id == NOTICE_2:
                raise RuntimeError("LLM 호출 실패 시뮬레이션")
            return _result()

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(
            warm_module,
            "explain_item",
            lambda conn_arg, item_id, *, force_refresh=False: _result(),
        )

        exit_code = warm_cache_script.main(["--db", str(db_path)])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert NOTICE_2 in captured.out
        assert "실패 1건" in captured.out

    def test_all_targets_failing_like_missing_api_key_does_not_crash_and_exits_one(
        self, tmp_path, monkeypatch, capsys
    ):
        db_path = tmp_path / "cli_all_fail.db"
        self._seed_db(db_path)

        def fake_process_notice(conn_arg, notice_id, *, force_refresh=False):
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        def fake_explain_item(conn_arg, item_id, *, force_refresh=False):
            raise RuntimeError("ANTHROPIC_API_KEY not set")

        monkeypatch.setattr(warm_module, "process_notice", fake_process_notice)
        monkeypatch.setattr(warm_module, "explain_item", fake_explain_item)

        exit_code = warm_cache_script.main(["--db", str(db_path)])

        captured = capsys.readouterr()
        assert exit_code == 1
        assert "실패 3건" in captured.out
        assert "캐시 적중: 0건" in captured.out

    def test_scope_notices_flag_never_calls_explain_item(self, tmp_path, monkeypatch):
        db_path = tmp_path / "cli_scope_notices.db"
        self._seed_db(db_path)

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("--scope notices는 explain_item을 호출하면 안 된다")

        monkeypatch.setattr(
            warm_module,
            "process_notice",
            lambda conn_arg, notice_id, *, force_refresh=False: _result(),
        )
        monkeypatch.setattr(warm_module, "explain_item", _must_not_be_called)

        exit_code = warm_cache_script.main(["--db", str(db_path), "--scope", "notices"])

        assert exit_code == 0

    def test_missing_db_flag_exits_two_via_argparse(self):
        with pytest.raises(SystemExit) as exc_info:
            warm_cache_script.main([])

        assert exc_info.value.code == 2

    def test_invalid_scope_value_exits_two_via_argparse(self, tmp_path):
        db_path = tmp_path / "cli_invalid_scope.db"
        self._seed_db(db_path)

        with pytest.raises(SystemExit) as exc_info:
            warm_cache_script.main(["--db", str(db_path), "--scope", "bogus"])

        assert exc_info.value.code == 2
