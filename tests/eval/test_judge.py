"""eval/judge.py 테스트(Task S-27) — 전부 모킹, 실 API·표준 DB 불요.

judge_generation은 medsupply.llm.explanation 테스트(test_explanation.py)와 동일한 모킹
관례(_FakeCompleteJson으로 medsupply.llm.client.complete_json 자리를 대신)를 따른다 —
조립 로직(교차 provider 바인딩·cache_key·JudgeScore 조립)만 검증하고 실제 LLM은 호출하지
않는다. run_experiment는 judge 모듈 안의 generate_risk_explanation·judge_generation 둘 다를
모킹해 순수 오케스트레이션(jsonl 기록·요약 산식·실패 격리)만 검증한다 — 표준 DB는 열지
않는다(케이스에 동봉된 evidence만 사용).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval import judge as judge_module
from eval.judge import check_completeness, judge_generation, run_experiment
from eval.schemas import JudgeOutput, JudgeScore
from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import LLMResult, RenderedPrompt
from medsupply.llm.explanation import ExplanationResult
from medsupply.llm.schemas import RiskExplanation

REPO_ROOT = Path(__file__).resolve().parents[2]
RUBRIC_TEXT = (REPO_ROOT / "eval" / "rubric.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 격리 — eval/results/를 실제로 건드리지 않도록 모든 테스트에서 RESULTS_DIR을 tmp_path로.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_results_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(judge_module, "RESULTS_DIR", tmp_path / "results")


# ---------------------------------------------------------------------------
# 공용 fixture 데이터
# ---------------------------------------------------------------------------


def _evidence_dict(**overrides) -> dict:
    base = dict(
        item_id="ITM-0001",
        item_name="세프트리악손주 1g(한국제약)",
        ingredient_name_kr="세프트리악손나트륨",
        as_of="2026-08-01",
        run_id="2026-08-01#a1b2c3d4",
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
        active_notices=[],
        next_shipment=None,
        substitutes_same_condition=[],
        evidence_refs=["risk:2026-08-01#a1b2c3d4", "usage:recent28", "stock:current"],
    )
    base.update(overrides)
    return base


def _case(**overrides) -> dict:
    base = dict(
        case_id="EC-ITM-0001",
        item_id="ITM-0001",
        run_id="2026-08-01#a1b2c3d4",
        is_pilot=True,
        evidence=_evidence_dict(),
        history=[],
    )
    base.update(overrides)
    return base


def _explanation_dict(**overrides) -> dict:
    base = dict(
        cause_summary="공급중단 공고로 재고 소진이 임박했다.",
        actions=[
            {
                "title": "대체 확보",
                "description": "대체군 재고를 확인한다.",
                "evidence_refs": ["stock:current"],
            }
        ],
        evidence_refs=["risk:2026-08-01#a1b2c3d4"],
        history_note=None,
    )
    base.update(overrides)
    return base


def _generation(**overrides) -> dict:
    base = {
        "explanation": _explanation_dict(),
        "meta": {"provider": "anthropic", "model": "claude-opus-5", "prompt_version": "v1"},
    }
    base.update(overrides)
    return base


def _judge_output(**overrides) -> JudgeOutput:
    base = dict(
        groundedness=0.9,
        cause_relevance=0.85,
        actionability=0.8,
        hallucination=False,
        rationale="근거를 정확히 인용했다.",
    )
    base.update(overrides)
    return JudgeOutput(**base)


def _judge_score(**overrides) -> JudgeScore:
    base = dict(
        groundedness=0.9,
        cause_relevance=0.85,
        actionability=0.8,
        hallucination=False,
        rationale="근거를 정확히 인용했다.",
        judge_model="gpt-5-test",
        rubric_version="v1",
    )
    base.update(overrides)
    return JudgeScore(**base)


def _explanation_result(*, explanation_kwargs=None, flags=(), provider="anthropic", model="claude-opus-5") -> ExplanationResult:
    return ExplanationResult(
        explanation=RiskExplanation(**_explanation_dict(**(explanation_kwargs or {}))),
        hallucination_flags=tuple(flags),
        provider=provider,
        model=model,
        prompt_version="v1",
        cache_hit=False,
    )


class _FakeCompleteJson:
    """medsupply.llm.explanation 테스트와 동일한 패턴 — complete_json 자리를 대신하는 페이크."""

    def __init__(self, data, *, provider="openai", model="gpt-5-test", cache_hit=False):
        self._result = LLMResult(
            data=data,
            provider=provider,
            model=model,
            cache_hit=cache_hit,
            latency_ms=0,
            trace_id=None,
            usage={"input_tokens": 1, "output_tokens": 1},
        )
        self.calls: list[dict] = []

    def __call__(self, task, prompt, schema, **kwargs):
        self.calls.append({"task": task, "prompt": prompt, "schema": schema, **kwargs})
        return self._result


TEST_CONFIG_TEXT = (
    "rubric_version: v1\n"
    "prompt_version: judge_v1\n"
    "temperature: 0\n"
    "judge_by_generation_provider:\n"
    "  anthropic: {provider: openai, model: gpt-5-snap}\n"
    "  openai: {provider: anthropic, model: claude-opus-5-snap}\n"
    "dataset:\n"
    "  cases: 40\n"
    "  pilot: 4\n"
    "  content_hash: null\n"
)


@pytest.fixture()
def config_path(tmp_path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(TEST_CONFIG_TEXT, encoding="utf-8")
    return path


def _write_dataset(tmp_path: Path, cases: list[dict]) -> Path:
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {"meta": {"dataset_version": "eval_cases_v1", "case_count": len(cases), "pilot_ids": []}, "cases": cases},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# judge_generation — 교차 provider 바인딩
# ---------------------------------------------------------------------------


class TestJudgeGenerationCrossProviderBinding:
    def test_anthropic_generation_routes_to_openai_judge(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        judge_generation(
            _case(),
            _generation(meta={"provider": "anthropic", "model": "claude-opus-5", "prompt_version": "v1"}),
            config_path=config_path,
        )

        assert len(fake.calls) == 1
        assert fake.calls[0]["provider"] == "openai"

    def test_openai_generation_routes_to_anthropic_judge(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        judge_generation(
            _case(),
            _generation(meta={"provider": "openai", "model": "gpt-5", "prompt_version": "v1"}),
            config_path=config_path,
        )

        assert fake.calls[0]["provider"] == "anthropic"

    def test_schema_passed_is_judge_output_not_judge_score(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        judge_generation(_case(), _generation(), config_path=config_path)

        assert fake.calls[0]["schema"] is JudgeOutput


# ---------------------------------------------------------------------------
# judge_generation — cache_key 결정성
# ---------------------------------------------------------------------------


class TestJudgeGenerationCacheKey:
    def test_deterministic_for_same_case_and_generation(self, monkeypatch, config_path):
        fake_a = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_a)
        judge_generation(_case(), _generation(), config_path=config_path)

        fake_b = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_b)
        judge_generation(_case(), _generation(), config_path=config_path)

        assert fake_a.calls[0]["cache_key"] == fake_b.calls[0]["cache_key"]

    def test_matches_build_cache_key_formula(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        case = _case()
        generation = _generation()
        judge_generation(case, generation, config_path=config_path)

        expected = build_cache_key(
            "judge",
            "v1",
            "gpt-5-snap",
            JudgeOutput,
            {"case_id": case["case_id"], "generation": generation, "rubric_version": "v1"},
        )
        assert fake.calls[0]["cache_key"] == expected

    def test_different_case_id_yields_different_cache_key(self, monkeypatch, config_path):
        fake_a = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_a)
        judge_generation(_case(case_id="EC-ITM-0001"), _generation(), config_path=config_path)

        fake_b = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_b)
        judge_generation(_case(case_id="EC-ITM-0002"), _generation(), config_path=config_path)

        assert fake_a.calls[0]["cache_key"] != fake_b.calls[0]["cache_key"]

    def test_different_generation_yields_different_cache_key(self, monkeypatch, config_path):
        fake_a = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_a)
        judge_generation(_case(), _generation(), config_path=config_path)

        fake_b = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake_b)
        judge_generation(
            _case(),
            _generation(explanation=_explanation_dict(cause_summary="다른 설명")),
            config_path=config_path,
        )

        assert fake_a.calls[0]["cache_key"] != fake_b.calls[0]["cache_key"]


# ---------------------------------------------------------------------------
# judge_generation — JudgeScore 조립·프롬프트 렌더
# ---------------------------------------------------------------------------


class TestJudgeGenerationAssembly:
    def test_returns_judge_score_with_model_and_rubric_version_filled(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output(groundedness=0.77), model="gpt-5-2026-08-01", provider="openai")
        monkeypatch.setattr(judge_module, "complete_json", fake)

        score = judge_generation(_case(), _generation(), config_path=config_path)

        assert isinstance(score, JudgeScore)
        assert score.groundedness == 0.77
        # judge_model은 config의 별칭이 아니라 실제로 호출된 모델 문자열이어야 한다(eval/schemas.py 계약).
        assert score.judge_model == "gpt-5-2026-08-01"
        assert score.rubric_version == "v1"

    def test_prompt_contains_rubric_case_context_and_generation_text(self, monkeypatch, config_path):
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        case = _case()
        generation = _generation()
        judge_generation(case, generation, config_path=config_path)

        prompt = fake.calls[0]["prompt"]
        assert isinstance(prompt, RenderedPrompt)
        assert prompt.version == "judge_v1"
        assert RUBRIC_TEXT in prompt.user
        assert json.dumps(case["evidence"], ensure_ascii=False, indent=2) in prompt.user
        assert json.dumps(generation["explanation"], ensure_ascii=False, indent=2) in prompt.user

    def test_other_case_fields_do_not_leak_into_case_context(self, monkeypatch, config_path):
        """case_context는 evidence(생성문이 근거로 삼아야 하는 데이터)만 담는다 — case_id 등
        평가 부기 필드는 근거가 아니므로 본문에 그대로 노출되지 않아야 한다."""
        fake = _FakeCompleteJson(_judge_output())
        monkeypatch.setattr(judge_module, "complete_json", fake)

        judge_generation(_case(is_pilot=True), _generation(), config_path=config_path)

        prompt = fake.calls[0]["prompt"]
        assert "is_pilot" not in prompt.user


# ---------------------------------------------------------------------------
# judge_generation — config 미존재·항목 결손 에러
# ---------------------------------------------------------------------------


class TestJudgeGenerationConfigErrors:
    def test_missing_config_file_raises(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        with pytest.raises(FileNotFoundError):
            judge_generation(_case(), _generation(), config_path=missing)

    def test_unknown_generation_provider_raises_value_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "rubric_version: v1\nprompt_version: judge_v1\n"
            "judge_by_generation_provider:\n  anthropic: {provider: openai, model: gpt-5}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            judge_generation(
                _case(),
                _generation(meta={"provider": "openai", "model": "x", "prompt_version": "v1"}),
                config_path=path,
            )

    def test_incomplete_entry_missing_model_key_raises_value_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(
            "rubric_version: v1\nprompt_version: judge_v1\n"
            "judge_by_generation_provider:\n  anthropic: {provider: openai}\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            judge_generation(_case(), _generation(), config_path=path)

    def test_missing_mapping_key_entirely_raises_value_error(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("rubric_version: v1\nprompt_version: judge_v1\n", encoding="utf-8")
        with pytest.raises(ValueError):
            judge_generation(_case(), _generation(), config_path=path)


class TestJudgeGenerationDefaultConfigIntegration:
    """실 eval/config.yaml(S-18·S-26 산출물)과의 호환성 — config_path 기본값 경로."""

    def test_default_config_path_loads_real_eval_config(self, monkeypatch):
        fake = _FakeCompleteJson(_judge_output(), model="gpt-5-real-test")
        monkeypatch.setattr(judge_module, "complete_json", fake)

        score = judge_generation(
            _case(), _generation(meta={"provider": "anthropic", "model": "claude-opus-5", "prompt_version": "v1"})
        )

        assert fake.calls[0]["provider"] == "openai"
        assert score.rubric_version == "v1"


# ---------------------------------------------------------------------------
# check_completeness — 4종 검사
# ---------------------------------------------------------------------------


class TestCheckCompleteness:
    def _risk_row(self, **overrides) -> dict:
        base = {"grade": "위험", "score": 92}
        base.update(overrides)
        return base

    def test_complete_explanation_yields_no_violations(self):
        assert check_completeness(self._risk_row(), _explanation_dict()) == []

    def test_missing_grade_empty_string_flagged(self):
        violations = check_completeness(self._risk_row(grade=""), _explanation_dict())
        assert any("grade" in v for v in violations)

    def test_missing_grade_key_entirely_flagged(self):
        violations = check_completeness({}, _explanation_dict())
        assert any("grade" in v for v in violations)

    def test_missing_cause_summary_flagged(self):
        violations = check_completeness(self._risk_row(), _explanation_dict(cause_summary=""))
        assert any("cause_summary" in v for v in violations)

    def test_whitespace_only_cause_summary_flagged(self):
        violations = check_completeness(self._risk_row(), _explanation_dict(cause_summary="   "))
        assert any("cause_summary" in v for v in violations)

    def test_missing_actions_flagged(self):
        violations = check_completeness(self._risk_row(), _explanation_dict(actions=[]))
        assert any("actions" in v and "evidence_refs" not in v for v in violations)

    def test_missing_top_level_evidence_refs_flagged(self):
        violations = check_completeness(self._risk_row(), _explanation_dict(evidence_refs=[]))
        assert any(v.startswith("missing_evidence_refs") and "actions[" not in v for v in violations)

    def test_missing_action_evidence_refs_flagged_per_action(self):
        explanation = _explanation_dict(
            actions=[
                {"title": "A", "description": "d", "evidence_refs": ["stock:current"]},
                {"title": "B", "description": "d2", "evidence_refs": []},
            ]
        )
        violations = check_completeness(self._risk_row(), explanation)
        assert any("actions[1]" in v for v in violations)
        assert not any("actions[0]" in v for v in violations)

    def test_multiple_violations_all_reported_independently(self):
        violations = check_completeness({}, {"cause_summary": "", "actions": [], "evidence_refs": []})
        # grade, cause_summary, actions(0건이라 per-action 위반은 발생하지 않음), top evidence_refs
        assert len(violations) == 4

    def test_returns_list_type(self):
        assert isinstance(check_completeness(self._risk_row(), _explanation_dict()), list)


# ---------------------------------------------------------------------------
# run_experiment — 모킹 흐름(jsonl 기록·요약 산식·실패 격리)
# ---------------------------------------------------------------------------


class TestRunExperiment:
    def _dataset_cases(self, n: int) -> list[dict]:
        return [
            _case(case_id=f"EC-ITM-{i:04d}", item_id=f"ITM-{i:04d}", evidence=_evidence_dict(item_id=f"ITM-{i:04d}"))
            for i in range(1, n + 1)
        ]

    def _mock_explain_ok(self, monkeypatch, **kwargs):
        monkeypatch.setattr(
            judge_module,
            "generate_risk_explanation",
            lambda evidence, **kw: _explanation_result(**kwargs),
        )

    def test_pilot_limit_processes_only_first_n_cases(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(6))
        self._mock_explain_ok(monkeypatch)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("pilot-run", prompt_version="v1", dataset_path=dataset_path, limit=4)

        assert summary["cases"] == 4
        assert summary["judged"] == 4

        results_file = judge_module.RESULTS_DIR / "pilot-run.jsonl"
        lines = results_file.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 4
        recorded_ids = [json.loads(line)["case_id"] for line in lines]
        assert recorded_ids == ["EC-ITM-0001", "EC-ITM-0002", "EC-ITM-0003", "EC-ITM-0004"]

    def test_no_limit_processes_all_cases(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(3))
        self._mock_explain_ok(monkeypatch)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("full-run", prompt_version="v1", dataset_path=dataset_path)

        assert summary["cases"] == 3
        assert summary["judged"] == 3

    def test_jsonl_line_shape_has_case_id_scores_flags_completeness(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(1))
        self._mock_explain_ok(monkeypatch, flags=("unsupported_number: 5",))
        monkeypatch.setattr(
            judge_module, "judge_generation", lambda case, generation, **kw: _judge_score(groundedness=0.5)
        )

        run_experiment("shape-run", prompt_version="v1", dataset_path=dataset_path)

        line = (judge_module.RESULTS_DIR / "shape-run.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(line)
        assert set(record.keys()) == {"case_id", "scores", "flags", "completeness"}
        assert record["case_id"] == "EC-ITM-0001"
        assert record["scores"]["groundedness"] == 0.5
        assert record["scores"]["judge_model"] == "gpt-5-test"
        assert record["flags"] == ["unsupported_number: 5"]
        assert record["completeness"] == []

    def test_completeness_violations_appear_on_incomplete_explanation(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(1))
        self._mock_explain_ok(monkeypatch, explanation_kwargs={"actions": []})
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        run_experiment("incomplete-run", prompt_version="v1", dataset_path=dataset_path)

        line = (judge_module.RESULTS_DIR / "incomplete-run.jsonl").read_text(encoding="utf-8").splitlines()[0]
        record = json.loads(line)
        assert any("actions" in v for v in record["completeness"])

    def test_summary_formula_matches_hand_calculation(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(3))
        self._mock_explain_ok(monkeypatch)

        scores = [
            _judge_score(groundedness=1.0, cause_relevance=1.0, actionability=1.0, hallucination=True),
            _judge_score(groundedness=0.5, cause_relevance=0.6, actionability=0.7, hallucination=False),
            _judge_score(groundedness=0.0, cause_relevance=0.2, actionability=0.4, hallucination=False),
        ]
        call_count = {"n": 0}

        def fake_judge(case, generation, **kw):
            score = scores[call_count["n"]]
            call_count["n"] += 1
            return score

        monkeypatch.setattr(judge_module, "judge_generation", fake_judge)

        summary = run_experiment("summary-run", prompt_version="v1", dataset_path=dataset_path)

        assert summary["cases"] == 3
        assert summary["judged"] == 3
        assert summary["hallucination_rate"] == round(1 / 3, 4)
        assert summary["mean_groundedness"] == round((1.0 + 0.5 + 0.0) / 3, 4)
        assert summary["mean_cause_relevance"] == round((1.0 + 0.6 + 0.2) / 3, 4)
        assert summary["mean_actionability"] == round((1.0 + 0.7 + 0.4) / 3, 4)
        assert summary["completeness_violations"] == 0

    def test_summary_keys_are_exactly_the_specified_seven(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(1))
        self._mock_explain_ok(monkeypatch)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("keys-run", prompt_version="v1", dataset_path=dataset_path)

        assert set(summary.keys()) == {
            "cases",
            "judged",
            "hallucination_rate",
            "mean_groundedness",
            "mean_cause_relevance",
            "mean_actionability",
            "completeness_violations",
        }

    def test_failed_case_is_isolated_and_recorded_without_aborting_batch(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(3))

        def flaky_explain(evidence, **kw):
            if evidence.item_id == "ITM-0002":
                raise RuntimeError("boom")
            return _explanation_result()

        monkeypatch.setattr(judge_module, "generate_risk_explanation", flaky_explain)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("failure-run", prompt_version="v1", dataset_path=dataset_path)

        assert summary["cases"] == 3
        assert summary["judged"] == 2

        lines = [
            json.loads(line)
            for line in (judge_module.RESULTS_DIR / "failure-run.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert len(lines) == 3  # 실패 건도 1줄로 기록된다("격리·기록")
        failed = next(r for r in lines if r["case_id"] == "EC-ITM-0002")
        assert "error" in failed and "boom" in failed["error"]
        assert failed["scores"] is None
        succeeded_ids = [r["case_id"] for r in lines if "error" not in r]
        assert succeeded_ids == ["EC-ITM-0001", "EC-ITM-0003"]

    def test_judge_generation_failure_is_also_isolated(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(2))
        self._mock_explain_ok(monkeypatch)

        def flaky_judge(case, generation, **kw):
            if case["case_id"] == "EC-ITM-0001":
                raise RuntimeError("judge boom")
            return _judge_score()

        monkeypatch.setattr(judge_module, "judge_generation", flaky_judge)

        summary = run_experiment("judge-failure-run", prompt_version="v1", dataset_path=dataset_path)

        assert summary["cases"] == 2
        assert summary["judged"] == 1

    def test_all_cases_fail_yields_none_means_and_zero_violations(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(2))

        def always_fail(evidence, **kw):
            raise RuntimeError("all fail")

        monkeypatch.setattr(judge_module, "generate_risk_explanation", always_fail)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("all-fail-run", prompt_version="v1", dataset_path=dataset_path)

        assert summary["cases"] == 2
        assert summary["judged"] == 0
        assert summary["hallucination_rate"] is None
        assert summary["mean_groundedness"] is None
        assert summary["mean_cause_relevance"] is None
        assert summary["mean_actionability"] is None
        assert summary["completeness_violations"] == 0

    def test_force_refresh_propagated_to_generate_risk_explanation(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(1))
        captured: dict = {}

        def fake_explain(evidence, *, history=(), prompt_version=None, force_refresh=False):
            captured["force_refresh"] = force_refresh
            return _explanation_result()

        monkeypatch.setattr(judge_module, "generate_risk_explanation", fake_explain)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        run_experiment("fr-run", prompt_version="v1", dataset_path=dataset_path, force_refresh=True)

        assert captured["force_refresh"] is True

    def test_prompt_version_propagated_to_generate_risk_explanation(self, monkeypatch, tmp_path):
        dataset_path = _write_dataset(tmp_path, self._dataset_cases(1))
        captured: dict = {}

        def fake_explain(evidence, *, history=(), prompt_version=None, force_refresh=False):
            captured["prompt_version"] = prompt_version
            return _explanation_result()

        monkeypatch.setattr(judge_module, "generate_risk_explanation", fake_explain)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        run_experiment("pv-run", prompt_version="v2-test", dataset_path=dataset_path)

        assert captured["prompt_version"] == "v2-test"

    def test_case_history_is_passed_through_to_explain(self, monkeypatch, tmp_path):
        history = [{"created_at": "2026-07-20T10:00:00", "action_type": "대체 검토", "note": "메모", "status": "완료"}]
        dataset_path = _write_dataset(tmp_path, [_case(history=history)])
        captured: dict = {}

        def fake_explain(evidence, *, history=(), prompt_version=None, force_refresh=False):
            captured["history"] = history
            return _explanation_result()

        monkeypatch.setattr(judge_module, "generate_risk_explanation", fake_explain)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        run_experiment("history-run", prompt_version="v1", dataset_path=dataset_path)

        assert list(captured["history"]) == history

    def test_default_dataset_path_loads_real_eval_cases_file(self, monkeypatch):
        """S-26 산출물(eval/cases/eval_cases_v1.json)과의 실제 호환성 — RiskEvidence 파싱까지
        전부 통과하는지 확인한다(judge_generation·generate_risk_explanation만 모킹)."""
        self._mock_explain_ok(monkeypatch)
        monkeypatch.setattr(judge_module, "judge_generation", lambda case, generation, **kw: _judge_score())

        summary = run_experiment("real-dataset-pilot", prompt_version="v1", limit=4)

        assert summary["cases"] == 4
        assert summary["judged"] == 4
