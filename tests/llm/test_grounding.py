"""근거 패키징(M-20) 테스트 — collect_risk_evidence + verify_explanation_grounding.

LLM 호출은 이 태스크에 없다(전부 결정적 코드). TestCollectRiskEvidence는 fixture_conn(tests/
conftest.py) 위에 시나리오별 최소 데이터를 직접 INSERT해 근거 패키징의 조립 로직을 검증하고,
TestVerifyExplanationGrounding은 RiskEvidence/RiskExplanation을 순수 Python 값으로 구성해
결정적 대조기의 5종 플래그를 검증한다(DB 불필요).
"""

from __future__ import annotations

import json

import pytest

from medsupply.llm.grounding import collect_risk_evidence, verify_explanation_grounding
from medsupply.llm.schemas import RiskAction, RiskEvidence, RiskExplanation
from tests.conftest import (
    AS_OF_TODAY,
    ITEM_1,
    ITEM_2,
    ITEM_3,
    NOTICE_HALT,
    RUN_TODAY,
    RUN_YESTERDAY,
)

# --------------------------------------------------------------------------
# 공용 헬퍼 — fixture_conn에 시나리오별 risk_results/공고 행을 직접 INSERT.
# --------------------------------------------------------------------------


def _insert_risk_result(
    conn,
    *,
    run_id: str,
    item_id: str,
    as_of: str = AS_OF_TODAY,
    grade: str = "주의",
    base_grade: str = "주의",
    escalated_by_notice: bool = False,
    risk_type: str = "general",
    score: int = 30,
    days_to_stockout: int | None = None,
    depletion_date: str | None = None,
    factors: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO risk_results(run_id, item_id, as_of, grade, base_grade,"
        " escalated_by_notice, risk_type, score, days_to_stockout, depletion_date, factors_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id, item_id, as_of, grade, base_grade, int(escalated_by_notice),
            risk_type, score, days_to_stockout, depletion_date,
            json.dumps(factors or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def _insert_active_notice(
    conn,
    *,
    notice_id: str,
    item_id: str,
    published_date: str,
    notice_type: str = "공급중단",
    reason: str = "사유 미상",
    substitute_group_id: str | None = None,
) -> None:
    """활성 판정을 만족하는(만료일 없음) 공고 1건을 notices+notice_extractions+notice_item_map에 채운다."""
    conn.execute(
        "INSERT INTO notices(notice_id, published_date, title, source, source_url, raw_text,"
        " notice_type, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id, published_date, f"{notice_id} 공고", "의약품통합정보시스템",
            f"https://example.invalid/notice/{notice_id}", "원문 생략", notice_type,
            f"{published_date}T09:00:00",
        ),
    )
    conn.execute(
        "INSERT INTO notice_extractions(notice_id, payload_json, confidence, status,"
        " prompt_version, provider, model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            notice_id,
            json.dumps(
                {
                    "product_names": [], "ingredient_names": [], "reason": reason,
                    "halt_start_date": None, "expected_restart_date": None,
                    "notice_type": notice_type, "evidence_quotes": [],
                },
                ensure_ascii=False,
            ),
            0.9, "자동확정", "notice_extract@v1", "anthropic", "claude-opus-5",
            f"{published_date}T09:30:00",
        ),
    )
    conn.execute(
        "INSERT INTO notice_item_map(notice_id, item_id, substitute_group_id, match_basis,"
        " needs_review) VALUES (?, ?, ?, ?, ?)",
        (notice_id, item_id, substitute_group_id, "standard_code", 0),
    )
    conn.commit()


# --------------------------------------------------------------------------
# TestCollectRiskEvidence
# --------------------------------------------------------------------------


class TestCollectRiskEvidence:
    """collect_risk_evidence(conn, item_id, run_id=None) -> RiskEvidence."""

    def test_happy_path_with_active_notice_shipment_and_substitute_has_all_fields(
        self, fixture_conn
    ) -> None:
        """ITEM_1/RUN_TODAY(기본 시드) — run_id 생략(기본 None=최신) 경로.

        기본 시드에 이미 활성 공고(NOTICE_HALT)·미입고 예정 건·같은 대체군 품목(ITEM_2)이
        갖춰져 있어 추가 INSERT 없이 "필드 완비 + refs 전집합"을 한번에 검증할 수 있다.
        """
        evidence = collect_risk_evidence(fixture_conn, ITEM_1)

        assert evidence.model_dump() == {
            "item_id": ITEM_1,
            "item_name": "세프트리악손주 1g(한국제약)",
            "ingredient_name_kr": "세프트리악손나트륨",
            "as_of": AS_OF_TODAY,
            "run_id": RUN_TODAY,
            "grade": "위험",
            "score": 92,
            "risk_type": "supply_halt",
            "days_to_stockout": 5,
            "depletion_date": "2026-08-06",
            "current_stock": 80.0,
            "avg_daily_usage": 10.0,
            "usage_change_pct": None,
            "anomalies": [],
            "escalated_by_notice": True,
            "active_notices": [
                {
                    "notice_id": NOTICE_HALT,
                    "title": "세프트리악손주 공급중단 안내",
                    "notice_type": "공급중단",
                    "published_date": "2026-07-15",
                    "reason": "제조소 설비 점검",
                }
            ],
            "next_shipment": {"expected_date": "2026-08-05", "qty": 200},
            "substitutes_same_condition": [
                {
                    "item_id": ITEM_2,
                    "item_name": "세프트리악손주 1g(대한제약)",
                    "supplier": "대한제약",
                    "current_stock": 40,
                }
            ],
            "evidence_refs": [
                f"risk:{RUN_TODAY}",
                "usage:recent28",
                "stock:current",
                f"notice:{NOTICE_HALT}",
                "shipment:1",
                f"substitute:{ITEM_2}",
            ],
        }

    def test_no_active_notice_and_no_shipment_path_includes_anomaly_refs(
        self, fixture_conn
    ) -> None:
        """ITEM_3 — notice_item_map·incoming_shipments·타 대체군 품목이 전혀 없는 품목.

        run_id를 명시(RUN_TODAY)해 인자 전달 경로도 함께 검증하고, factors_json에 anomalies를
        직접 채워 anomaly:{kind}:{detected_on} 참조 형식을 확인한다.
        """
        _insert_risk_result(
            fixture_conn,
            run_id=RUN_TODAY,
            item_id=ITEM_3,
            grade="주의",
            base_grade="주의",
            escalated_by_notice=False,
            risk_type="demand_surge",
            score=35,
            days_to_stockout=40,
            depletion_date="2026-09-10",
            factors={
                "anomalies": [
                    {
                        "kind": "usage_surge",
                        "detected_on": "2026-07-29",
                        "metric": 0.41,
                        "detail": "최근 사용량 급증",
                    }
                ]
            },
        )

        evidence = collect_risk_evidence(fixture_conn, ITEM_3, run_id=RUN_TODAY)

        assert evidence.grade == "주의"
        assert evidence.escalated_by_notice is False
        assert evidence.current_stock == 52.0
        assert evidence.avg_daily_usage == 3.7
        assert evidence.usage_change_pct is None
        assert evidence.anomalies == [
            {
                "kind": "usage_surge",
                "detected_on": "2026-07-29",
                "metric": 0.41,
                "detail": "최근 사용량 급증",
            }
        ]
        assert evidence.active_notices == []
        assert evidence.next_shipment is None
        assert evidence.substitutes_same_condition == []
        assert evidence.evidence_refs == [
            f"risk:{RUN_TODAY}",
            "usage:recent28",
            "stock:current",
            "anomaly:usage_surge:2026-07-29",
        ]

    def test_active_notices_sorted_by_published_date_desc_then_notice_id_asc(
        self, fixture_conn
    ) -> None:
        """ITEM_2 — 기본 시드의 NOTICE_HALT(07-15) 외 2건을 추가해 정렬 규칙을 검증한다.

        기대 순서: NTC-0050(07-25, 최신) → NOTICE_HALT/NTC-0001(07-15) → NTC-0099(07-15, 동일
        날짜 tie-break는 notice_id 오름차순).
        """
        _insert_risk_result(
            fixture_conn,
            run_id=RUN_TODAY,
            item_id=ITEM_2,
            grade="경고",
            base_grade="경고",
            escalated_by_notice=True,
            risk_type="supply_halt",
            score=60,
            days_to_stockout=20,
            depletion_date="2026-08-21",
        )
        _insert_active_notice(
            fixture_conn, notice_id="NTC-0050", item_id=ITEM_2,
            published_date="2026-07-25", notice_type="공급부족", reason="원료 수급 차질",
        )
        _insert_active_notice(
            fixture_conn, notice_id="NTC-0099", item_id=ITEM_2,
            published_date="2026-07-15", notice_type="공급중단", reason="추가 사유",
        )

        evidence = collect_risk_evidence(fixture_conn, ITEM_2, run_id=RUN_TODAY)

        assert [n["notice_id"] for n in evidence.active_notices] == [
            "NTC-0050", NOTICE_HALT, "NTC-0099",
        ]
        assert evidence.evidence_refs == [
            f"risk:{RUN_TODAY}",
            "usage:recent28",
            "stock:current",
            "notice:NTC-0050",
            f"notice:{NOTICE_HALT}",
            "notice:NTC-0099",
            f"substitute:{ITEM_1}",
        ]

    def test_null_depletion_date_and_days_to_stockout_pass_through_as_none(
        self, fixture_conn
    ) -> None:
        """소진 예측이 없는(risk_type='general') 행 — TEXT NULL(depletion_date)과 INTEGER
        NULL(days_to_stockout)이 섞여도 둘 다 None으로 정상 변환되는지 확인한다."""
        _insert_risk_result(
            fixture_conn,
            run_id=RUN_TODAY,
            item_id=ITEM_2,
            grade="정상",
            base_grade="정상",
            score=10,
            days_to_stockout=None,
            depletion_date=None,
        )

        evidence = collect_risk_evidence(fixture_conn, ITEM_2, run_id=RUN_TODAY)

        assert evidence.days_to_stockout is None
        assert evidence.depletion_date is None

    def test_raises_value_error_when_run_id_does_not_exist(self, fixture_conn) -> None:
        with pytest.raises(ValueError, match=rf"{ITEM_1}.*no-such-run|no-such-run.*{ITEM_1}"):
            collect_risk_evidence(fixture_conn, ITEM_1, run_id="no-such-run")

    def test_raises_value_error_when_item_not_in_given_run(self, fixture_conn) -> None:
        """RUN_YESTERDAY에는 ITEM_1 행만 있고 ITEM_2 행은 없다."""
        with pytest.raises(ValueError, match=rf"{ITEM_2}.*{RUN_YESTERDAY}|{RUN_YESTERDAY}.*{ITEM_2}"):
            collect_risk_evidence(fixture_conn, ITEM_2, run_id=RUN_YESTERDAY)

    def test_same_input_twice_returns_equal_result(self, fixture_conn) -> None:
        first = collect_risk_evidence(fixture_conn, ITEM_1, run_id=RUN_TODAY)
        second = collect_risk_evidence(fixture_conn, ITEM_1, run_id=RUN_TODAY)

        assert first == second
        assert first.evidence_refs == second.evidence_refs


# --------------------------------------------------------------------------
# TestVerifyExplanationGrounding — DB 불필요(순수 값 대조).
# --------------------------------------------------------------------------


def _make_evidence(**overrides) -> RiskEvidence:
    """기본값은 아래 _make_explanation() 기본값과 완전히 정합하는(플래그 0건) 근거다."""
    defaults = dict(
        item_id=ITEM_1,
        item_name="세프트리악손주 1g(한국제약)",
        ingredient_name_kr="세프트리악손나트륨",
        as_of="2026-08-01",
        run_id=RUN_TODAY,
        grade="위험",
        score=92,
        risk_type="supply_halt",
        days_to_stockout=5,
        depletion_date="2026-08-06",
        current_stock=80.0,
        avg_daily_usage=10.0,
        usage_change_pct=-12.3,
        anomalies=[
            {
                "kind": "usage_surge", "detected_on": "2026-07-28", "metric": 0.41,
                "detail": "최근 사용량 급증",
            }
        ],
        escalated_by_notice=True,
        active_notices=[
            {
                "notice_id": NOTICE_HALT,
                "title": "세프트리악손주 공급중단 안내",
                "notice_type": "공급중단",
                "published_date": "2026-07-15",
                "reason": "제조소 설비 점검",
            }
        ],
        next_shipment={"expected_date": "2026-08-05", "qty": 200},
        substitutes_same_condition=[
            {
                "item_id": ITEM_2, "item_name": "세프트리악손주 1g(대한제약)",
                "supplier": "대한제약", "current_stock": 40,
            }
        ],
        evidence_refs=[
            f"risk:{RUN_TODAY}", "usage:recent28", "stock:current",
            "anomaly:usage_surge:2026-07-28", f"notice:{NOTICE_HALT}", "shipment:1",
            f"substitute:{ITEM_2}",
        ],
    )
    defaults.update(overrides)
    return RiskEvidence(**defaults)


def _make_explanation(**overrides) -> RiskExplanation:
    """기본값은 _make_evidence() 기본값과 완전히 정합하는(플래그 0건) 설명이다."""
    defaults = dict(
        cause_summary=(
            "공급중단 공고(2026-07-15)로 재고 소진이 임박했다. 현재 재고 80, 최근 일평균"
            " 사용량 10 수준으로 5일 내 소진 예상(2026-08-06)."
        ),
        actions=[
            RiskAction(
                title="대체 품목 확보",
                description="같은 대체군 품목의 확보를 검토한다.",
                evidence_refs=[f"substitute:{ITEM_2}"],
            ),
            RiskAction(
                title="입고 일정 확인",
                description="2026-08-05 입고 예정 200개를 확인한다.",
                evidence_refs=["shipment:1"],
            ),
        ],
        evidence_refs=[f"risk:{RUN_TODAY}", f"notice:{NOTICE_HALT}"],
    )
    defaults.update(overrides)
    return RiskExplanation(**defaults)


class TestVerifyExplanationGrounding:
    """verify_explanation_grounding(evidence, explanation) -> list[str]."""

    def test_fully_grounded_explanation_has_no_flags(self) -> None:
        flags = verify_explanation_grounding(_make_evidence(), _make_explanation())

        assert flags == []

    # -- 1. unknown_ref --------------------------------------------------

    def test_unknown_ref_flag_when_action_cites_id_outside_evidence(self) -> None:
        explanation = _make_explanation(
            actions=[
                RiskAction(
                    title="원인 재확인", description="추가 확인이 필요하다.",
                    evidence_refs=["notice:NTC-9999"],
                ),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == ["unknown_ref: notice:NTC-9999"]

    def test_unknown_ref_negative_when_all_cited_ids_are_in_evidence(self) -> None:
        flags = verify_explanation_grounding(_make_evidence(), _make_explanation())

        assert not any(f.startswith("unknown_ref") for f in flags)

    # -- 2. empty_refs -----------------------------------------------------

    def test_empty_refs_flag_for_top_level_and_each_empty_action(self) -> None:
        explanation = _make_explanation(
            evidence_refs=[],
            actions=[
                RiskAction(title="A", description="설명 A", evidence_refs=[]),
                RiskAction(title="B", description="설명 B", evidence_refs=[f"substitute:{ITEM_2}"]),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == [
            "empty_refs: explanation.evidence_refs",
            "empty_refs: actions[0].evidence_refs ('A')",
        ]

    def test_empty_refs_negative_when_top_level_and_all_actions_have_refs(self) -> None:
        flags = verify_explanation_grounding(_make_evidence(), _make_explanation())

        assert not any(f.startswith("empty_refs") for f in flags)

    # -- 3. unsupported_date ------------------------------------------------

    def test_unsupported_date_flag_when_body_cites_date_outside_evidence(self) -> None:
        explanation = _make_explanation(
            cause_summary="2026-09-01에 재평가가 필요하다.",
            actions=[
                RiskAction(title="A", description="설명.", evidence_refs=[f"risk:{RUN_TODAY}"]),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == ["unsupported_date: 2026-09-01"]

    def test_unsupported_date_negative_when_body_only_cites_evidence_dates(self) -> None:
        flags = verify_explanation_grounding(_make_evidence(), _make_explanation())

        assert not any(f.startswith("unsupported_date") for f in flags)

    # -- 4. unsupported_number -----------------------------------------------

    def test_unsupported_number_flag_when_body_cites_number_outside_evidence(self) -> None:
        explanation = _make_explanation(
            cause_summary="예상 소요 재고는 150 수준이다.",
            actions=[
                RiskAction(title="A", description="설명.", evidence_refs=[f"risk:{RUN_TODAY}"]),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == ["unsupported_number: 150"]

    def test_unsupported_number_flags_non_date_four_digit_number(self) -> None:
        """4자리 수라는 이유만으로 연도로 오인해 면제하지 않는다(날짜 구성부가 아니면 검사)."""
        explanation = _make_explanation(
            cause_summary="향후 소요량은 1500 정도로 평가된다.",
            actions=[
                RiskAction(title="A", description="설명.", evidence_refs=[f"risk:{RUN_TODAY}"]),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == ["unsupported_number: 1500"]

    def test_unsupported_number_ignores_bare_year_mention(self) -> None:
        explanation = _make_explanation(
            cause_summary="2026년 하반기 재평가가 필요하다.",
            actions=[
                RiskAction(title="A", description="설명.", evidence_refs=[f"risk:{RUN_TODAY}"]),
            ],
        )

        flags = verify_explanation_grounding(_make_evidence(), explanation)

        assert flags == []

    def test_unsupported_number_negative_allows_int_rounding_equivalence(self) -> None:
        """evidence는 9.6(소수1)인데 본문이 정수화한 10을 인용해도 무플래그(경계)."""
        evidence = _make_evidence(avg_daily_usage=9.6)

        flags = verify_explanation_grounding(evidence, _make_explanation())

        assert flags == []

    # -- 5. phantom_notice -------------------------------------------------

    def test_phantom_notice_flag_when_active_notices_empty_but_body_mentions_notice(self) -> None:
        """공고 언급 자체는 기본 설명(cause_summary)에서 물려받되, 그 공고의 published_date
        (2026-07-15)는 인용하지 않는 본문으로 바꿔 unsupported_date와 섞이지 않게 한다."""
        evidence = _make_evidence(active_notices=[])
        explanation = _make_explanation(
            cause_summary="관련 공고 내용을 검토해야 한다. 현재 재고 80, 최근 일평균 사용량 10.",
            actions=[
                RiskAction(title="확인", description="검토가 필요하다.", evidence_refs=[f"risk:{RUN_TODAY}"]),
            ],
        )

        flags = verify_explanation_grounding(evidence, explanation)

        assert flags == ["phantom_notice: active_notices empty but body mentions 공고"]

    def test_phantom_notice_negative_when_notice_present_and_mentioned(self) -> None:
        """경계: 활성 공고가 있는 상태에서 본문이 '공고'를 언급해도 무플래그."""
        flags = verify_explanation_grounding(_make_evidence(), _make_explanation())

        assert not any(f.startswith("phantom_notice") for f in flags)

    # -- 결정성 -------------------------------------------------------------

    def test_verify_is_deterministic_and_orders_flags_by_check_sequence(self) -> None:
        evidence = _make_evidence(active_notices=[])
        explanation = RiskExplanation(
            cause_summary="공고에 따르면 2026-09-01까지 150 확보가 필요하다.",
            actions=[
                RiskAction(title="확인", description="근거 없는 조치.", evidence_refs=[]),
                RiskAction(title="추가", description="설명.", evidence_refs=["notice:NTC-9999"]),
            ],
            evidence_refs=[],
        )

        first = verify_explanation_grounding(evidence, explanation)
        second = verify_explanation_grounding(evidence, explanation)

        assert first == second
        assert first == [
            "unknown_ref: notice:NTC-9999",
            "empty_refs: explanation.evidence_refs",
            "empty_refs: actions[0].evidence_refs ('확인')",
            "unsupported_date: 2026-09-01",
            "unsupported_number: 150",
            "phantom_notice: active_notices empty but body mentions 공고",
        ]
