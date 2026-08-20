"""원인 설명·대응방안 생성(M-21) 테스트 — 전부 모킹, 실 API 불요.

extraction.py(M-13)와 동일한 관례: LLM 응답(complete_json)은 페이크로 치환해 조립 로직만
검증하고, 환각 판정(verify_explanation_grounding)은 M-20의 결정적 코드를 실제로 그대로
호출해 "모킹 응답 → flags 결합"이 올바른지 확인한다. TestGenerateRiskExplanation은 DB
없이 RiskEvidence를 직접 구성해 generate_risk_explanation()만 검증하고,
TestExplainItem은 fixture_conn(tests/conftest.py) 위에서 explain_item()의 근거 수집 →
생성 → 영속화 전체 경로를 검증한다.
"""

from __future__ import annotations

import json
import os

import pytest

from medsupply.data import queries
from medsupply.llm import explanation as explanation_module
from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import LLMResult, RenderedPrompt
from medsupply.llm.config import load_llm_config
from medsupply.llm.explanation import ExplanationResult, explain_item, generate_risk_explanation
from medsupply.llm.grounding import collect_risk_evidence
from medsupply.llm.prompts.loader import PromptTemplate, list_prompts, load_prompt
from medsupply.llm.schemas import RiskAction, RiskEvidence, RiskExplanation
from tests.conftest import ITEM_1, ITEM_2, NOTICE_HALT, RUN_TODAY

# --------------------------------------------------------------------------
# 공용 픽스처 데이터 — ITEM_1/RUN_TODAY(기본 시드)와 정합하는 evidence/explanation.
# --------------------------------------------------------------------------


def _make_evidence(**overrides) -> RiskEvidence:
    """기본값은 conftest 기본 시드의 collect_risk_evidence(fixture_conn, ITEM_1) 결과와
    동일하다(tests/llm/test_grounding.py의 happy-path 어서션 참조)."""
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
        usage_change_pct=None,
        anomalies=[],
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
                "item_id": ITEM_2,
                "item_name": "세프트리악손주 1g(대한제약)",
                "supplier": "대한제약",
                "current_stock": 40,
            }
        ],
        evidence_refs=[
            f"risk:{RUN_TODAY}",
            "usage:recent28",
            "stock:current",
            f"notice:{NOTICE_HALT}",
            "shipment:1",
            f"substitute:{ITEM_2}",
        ],
    )
    defaults.update(overrides)
    return RiskEvidence(**defaults)


def _grounded_explanation() -> RiskExplanation:
    """_make_evidence() 기본값과 완전히 정합하는(플래그 0건) 설명."""
    return RiskExplanation(
        cause_summary=(
            "공급중단 공고(2026-07-15)로 재고 소진이 임박했다. 현재 재고 80, 최근 일평균"
            " 사용량 10 수준으로 5일 내 소진 예상(2026-08-06)."
        ),
        actions=[
            RiskAction(
                title="대체 품목 확보",
                description="같은 대체군 품목(재고 40)의 확보를 검토한다.",
                evidence_refs=[f"substitute:{ITEM_2}"],
            ),
        ],
        evidence_refs=[f"risk:{RUN_TODAY}", f"notice:{NOTICE_HALT}"],
    )


# --------------------------------------------------------------------------
# 페이크 complete_json / LLMResult 헬퍼(test_extraction.py와 동일 패턴).
# --------------------------------------------------------------------------


def _fake_llm_result(
    data: RiskExplanation,
    *,
    provider: str = "anthropic",
    model: str = "claude-opus-5",
    cache_hit: bool = False,
) -> LLMResult:
    return LLMResult(
        data=data,
        provider=provider,
        model=model,
        cache_hit=cache_hit,
        latency_ms=0,
        trace_id=None,
        usage={"input_tokens": 1, "output_tokens": 1},
    )


class _FakeCompleteJson:
    """medsupply.llm.explanation.complete_json 자리를 대신하는 콜 기록용 페이크."""

    def __init__(self, result: LLMResult):
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, task, prompt, schema, **kwargs):
        self.calls.append({"task": task, "prompt": prompt, "schema": schema, **kwargs})
        return self._result


# --------------------------------------------------------------------------
# TestGenerateRiskExplanation — generate_risk_explanation() 단위(DB 불필요).
# --------------------------------------------------------------------------


class TestGenerateRiskExplanation:
    def test_cache_key_is_deterministic_and_passed_to_complete_json(self, monkeypatch):
        evidence = _make_evidence()
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        generate_risk_explanation(evidence)

        assert len(fake.calls) == 1
        call = fake.calls[0]
        assert call["task"] == "risk_explain"
        assert call["schema"] is RiskExplanation
        assert isinstance(call["prompt"], RenderedPrompt)
        assert call["force_refresh"] is False

        cfg = load_llm_config()
        expected_key = build_cache_key(
            "risk_explain",
            "v1",
            cfg.anthropic_model,
            RiskExplanation,
            {"evidence": evidence.model_dump(), "history": []},
        )
        assert call["cache_key"] == expected_key

    def test_same_evidence_and_history_always_yield_same_cache_key(self, monkeypatch):
        evidence = _make_evidence()
        history = [
            {
                "created_at": "2026-07-20T10:00:00",
                "action_type": "대체 검토",
                "note": "대체 후보 확인",
                "status": "진행 중",
            }
        ]

        fake_a = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_a)
        generate_risk_explanation(evidence, history=history)

        fake_b = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_b)
        generate_risk_explanation(evidence, history=history)

        assert fake_a.calls[0]["cache_key"] == fake_b.calls[0]["cache_key"]

    def test_different_evidence_yields_different_cache_key(self, monkeypatch):
        fake_a = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_a)
        generate_risk_explanation(_make_evidence())

        fake_b = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_b)
        generate_risk_explanation(_make_evidence(score=10))

        assert fake_a.calls[0]["cache_key"] != fake_b.calls[0]["cache_key"]

    def test_different_history_yields_different_cache_key(self, monkeypatch):
        evidence = _make_evidence()

        fake_a = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_a)
        generate_risk_explanation(evidence, history=[])

        fake_b = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_b)
        generate_risk_explanation(
            evidence,
            history=[
                {
                    "created_at": "2026-07-20T10:00:00",
                    "action_type": "대체 검토",
                    "note": "메모",
                    "status": "완료",
                }
            ],
        )

        assert fake_a.calls[0]["cache_key"] != fake_b.calls[0]["cache_key"]

    def test_force_refresh_is_propagated_to_complete_json(self, monkeypatch):
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        generate_risk_explanation(_make_evidence(), force_refresh=True)

        assert fake.calls[0]["force_refresh"] is True

    def test_prompt_version_override_is_rendered_and_reflected_in_cache_key(self, monkeypatch):
        evidence = _make_evidence()
        fake_template = PromptTemplate(
            task="risk_explain",
            version="v2-test",
            system="너는 설명 도우미다.",
            user_template="근거: {evidence_json} / 이력: {history_json}",
        )
        captured_versions: list[str | None] = []

        def fake_load_prompt(task, version=None):
            assert task == "risk_explain"
            captured_versions.append(version)
            return fake_template

        monkeypatch.setattr(explanation_module, "load_prompt", fake_load_prompt)
        fake_complete = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake_complete)

        result = generate_risk_explanation(evidence, prompt_version="v2-test")

        assert captured_versions == ["v2-test"]
        assert result.prompt_version == "v2-test"

        call = fake_complete.calls[0]
        assert call["prompt"].version == "v2-test"

        cfg = load_llm_config()
        expected_key = build_cache_key(
            "risk_explain",
            "v2-test",
            cfg.anthropic_model,
            RiskExplanation,
            {"evidence": evidence.model_dump(), "history": []},
        )
        assert call["cache_key"] == expected_key

    def test_history_trimmed_to_recent_three_with_allowed_fields_only(self, monkeypatch):
        evidence = _make_evidence()
        history = [
            {
                "history_id": 4, "created_at": "2026-08-04T09:00:00", "item_id": ITEM_1,
                "action_type": "대체 검토", "owner": "약제부", "note": "4차 조치",
                "status": "완료", "order_id": None,
            },
            {
                "history_id": 3, "created_at": "2026-08-03T09:00:00", "item_id": ITEM_1,
                "action_type": "대체 검토", "owner": "약제부", "note": "3차 조치",
                "status": "완료", "order_id": None,
            },
            {
                "history_id": 2, "created_at": "2026-08-02T09:00:00", "item_id": ITEM_1,
                "action_type": "대체 검토", "owner": "약제부", "note": "2차 조치",
                "status": "완료", "order_id": None,
            },
            {
                "history_id": 1, "created_at": "2026-08-01T09:00:00", "item_id": ITEM_1,
                "action_type": "대체 검토", "owner": "약제부", "note": "1차 조치(제외 대상)",
                "status": "완료", "order_id": None,
            },
        ]
        fake_template = PromptTemplate(
            task="risk_explain", version="v1", system="시스템", user_template="{history_json}",
        )
        monkeypatch.setattr(explanation_module, "load_prompt", lambda task, version=None: fake_template)
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        generate_risk_explanation(evidence, history=history)

        rendered_history = json.loads(fake.calls[0]["prompt"].user)
        assert [h["note"] for h in rendered_history] == ["4차 조치", "3차 조치", "2차 조치"]
        assert all(
            set(h.keys()) == {"created_at", "action_type", "note", "status"} for h in rendered_history
        )

    def test_grounded_response_yields_empty_hallucination_flags(self, monkeypatch):
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        result = generate_risk_explanation(_make_evidence())

        assert result.hallucination_flags == ()

    def test_response_citing_number_outside_evidence_yields_unsupported_number_flag(
        self, monkeypatch
    ):
        ungrounded = RiskExplanation(
            cause_summary="예상 소요 재고는 150 수준이다.",
            actions=[
                RiskAction(
                    title="확인", description="추가 확인이 필요하다.",
                    evidence_refs=[f"risk:{RUN_TODAY}"],
                ),
            ],
            evidence_refs=[f"risk:{RUN_TODAY}"],
        )
        fake = _FakeCompleteJson(_fake_llm_result(ungrounded))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        result = generate_risk_explanation(_make_evidence())

        assert result.hallucination_flags == ("unsupported_number: 150",)

    def test_return_fields_are_assembled_from_llm_result(self, monkeypatch):
        explanation = _grounded_explanation()
        fake_result = _fake_llm_result(explanation, provider="openai", model="gpt-5", cache_hit=True)
        monkeypatch.setattr(explanation_module, "complete_json", _FakeCompleteJson(fake_result))

        result = generate_risk_explanation(_make_evidence())

        assert isinstance(result, ExplanationResult)
        assert result.explanation == explanation
        assert result.provider == "openai"
        assert result.model == "gpt-5"
        assert result.cache_hit is True
        assert result.prompt_version == "v1"

    def test_history_note_left_as_is_when_history_present_but_model_omits_it(self, monkeypatch):
        explanation = _grounded_explanation()
        assert explanation.history_note is None  # 스키마 기본값
        fake = _FakeCompleteJson(_fake_llm_result(explanation))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        history = [
            {
                "created_at": "2026-07-20T10:00:00", "action_type": "대체 검토",
                "note": "대체 후보 확인", "status": "진행 중",
            }
        ]
        result = generate_risk_explanation(_make_evidence(), history=history)

        # history가 비어 있지 않아도 history_note를 강제로 채우지 않는다(모델 재량).
        assert result.explanation.history_note is None


# --------------------------------------------------------------------------
# TestExplainItem — explain_item() 통합(fixture_conn + complete_json 모킹).
# --------------------------------------------------------------------------


class TestExplainItem:
    def test_persists_payload_with_explanation_and_flags_using_latest_run(
        self, fixture_conn, monkeypatch
    ):
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        result = explain_item(fixture_conn, ITEM_1)

        assert result.hallucination_flags == ()

        row = fixture_conn.execute(
            "SELECT item_id, run_id, payload_json, prompt_version, provider, model"
            " FROM llm_explanations WHERE item_id = ?",
            (ITEM_1,),
        ).fetchone()
        assert row is not None
        assert row["run_id"] == RUN_TODAY
        assert row["prompt_version"] == "v1"
        assert row["provider"] == "anthropic"
        assert row["model"] == "claude-opus-5"

        payload = json.loads(row["payload_json"])
        assert payload["hallucination_flags"] == []
        assert payload["explanation"] == _grounded_explanation().model_dump()

    def test_rerun_is_idempotent_single_row_via_insert_or_replace(self, fixture_conn, monkeypatch):
        monkeypatch.setattr(
            explanation_module, "complete_json",
            _FakeCompleteJson(_fake_llm_result(_grounded_explanation())),
        )
        explain_item(fixture_conn, ITEM_1)

        monkeypatch.setattr(
            explanation_module, "complete_json",
            _FakeCompleteJson(_fake_llm_result(_grounded_explanation())),
        )
        explain_item(fixture_conn, ITEM_1)

        count = fixture_conn.execute(
            "SELECT COUNT(*) FROM llm_explanations WHERE item_id = ?", (ITEM_1,)
        ).fetchone()[0]
        assert count == 1

    def test_raises_value_error_when_item_has_no_run(self, fixture_conn, monkeypatch):
        """ITEM_2는 기본 시드에서 어떤 run에도 risk_results 행이 없다."""
        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        with pytest.raises(ValueError):
            explain_item(fixture_conn, ITEM_2)

        assert fake.calls == []  # 근거 수집 실패 전에는 LLM을 호출하지 않는다.

    def test_history_limited_to_recent_three_when_more_than_three_exist(
        self, fixture_conn, monkeypatch
    ):
        for i, created_at in enumerate(
            [
                "2026-07-21T09:00:00", "2026-07-22T09:00:00",
                "2026-07-23T09:00:00", "2026-07-24T09:00:00",
            ],
            start=1,
        ):
            fixture_conn.execute(
                "INSERT INTO action_history(created_at, item_id, action_type, owner, note,"
                " status, risk_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (created_at, ITEM_1, "대체 검토", "약제부", f"{i}차 조치", "완료", "supply_halt"),
            )
        fixture_conn.commit()

        expected_records = queries.list_action_history(
            fixture_conn, item_id=ITEM_1, risk_type="supply_halt", limit=3
        ).to_dict(orient="records")
        expected_trimmed = [
            {field: record.get(field) for field in ("created_at", "action_type", "note", "status")}
            for record in expected_records
        ]
        assert [r["note"] for r in expected_trimmed] == ["4차 조치", "3차 조치", "2차 조치"]

        fake = _FakeCompleteJson(_fake_llm_result(_grounded_explanation()))
        monkeypatch.setattr(explanation_module, "complete_json", fake)

        explain_item(fixture_conn, ITEM_1)

        cfg = load_llm_config()
        evidence = collect_risk_evidence(fixture_conn, ITEM_1)
        expected_key = build_cache_key(
            "risk_explain", "v1", cfg.anthropic_model, RiskExplanation,
            {"evidence": evidence.model_dump(), "history": expected_trimmed},
        )
        assert fake.calls[0]["cache_key"] == expected_key

        user_text = fake.calls[0]["prompt"].user
        assert "4차 조치" in user_text
        assert "3차 조치" in user_text
        assert "2차 조치" in user_text
        assert "1차 조치" not in user_text


# --------------------------------------------------------------------------
# TestRiskExplainPromptV1 — 프롬프트 렌더·레지스트리(브리프 §프롬프트 v1 요건).
# --------------------------------------------------------------------------


class TestRiskExplainPromptV1:
    def test_registry_has_risk_explain_v1_registered(self):
        prompts = list_prompts()

        assert "risk_explain" in prompts
        assert prompts["risk_explain"]["active"] == "v1"
        assert "v1" in prompts["risk_explain"]["versions"]

    def test_render_includes_evidence_numbers_and_history_note(self):
        evidence = _make_evidence()
        history = [
            {
                "created_at": "2026-07-20T10:00:00", "action_type": "대체 검토",
                "note": "대체 후보 확인", "status": "진행 중",
            }
        ]

        rendered = load_prompt("risk_explain", "v1").render(
            evidence_json=json.dumps(evidence.model_dump(), ensure_ascii=False),
            history_json=json.dumps(history, ensure_ascii=False),
        )

        assert rendered.version == "v1"
        assert str(evidence.score) in rendered.user
        assert evidence.depletion_date in rendered.user
        assert "대체 후보 확인" in rendered.user

    def test_system_prompt_instructs_not_to_re_judge_grade_or_score(self):
        template = load_prompt("risk_explain", "v1")

        assert "판정" in template.system


# --------------------------------------------------------------------------
# (선택) 실 API 스모크 — 키가 없으면 CI 안전하게 skip
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="ANTHROPIC_API_KEY/OPENAI_API_KEY가 없으면 실제 LLM 설명 생성 스모크를 건너뛴다",
)
def test_smoke_real_explanation_for_item1(fixture_conn):
    result = explain_item(fixture_conn, ITEM_1, force_refresh=True)

    assert isinstance(result.explanation, RiskExplanation)
    assert result.provider in ("anthropic", "openai")
    assert isinstance(result.hallucination_flags, tuple)
