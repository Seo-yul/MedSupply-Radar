"""eval/ 루브릭 v1 산출물(루브릭 문서·judge 프롬프트·config·JudgeScore 스키마·목업 픽스처)
검증(Task S-18).

judge 실행기(eval/judge.py)는 Task S-27 몫이라 여기서 만들지 않는다 — 그래서 아래 테스트는
"실행 결과가 옳은가"가 아니라 "S-27 실행기가 조립할 재료가 계약대로 갖춰져 있는가"만 본다:
스키마 형태, config 필수 키·교차 judge 매핑, 프롬프트 렌더, 목업 픽스처의 판별력 설계.

유일한 예외는 맨 아래 (선택) 실 API 스모크 — API 키가 있을 때만 실제 judge 호출까지
수행한다. 이 환경(2026-08-21 기준)은 ANTHROPIC_API_KEY·OPENAI_API_KEY가 모두 없어
skipif로 건너뛴다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from eval.schemas import JudgeOutput, JudgeScore

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "eval"
RUBRIC_PATH = EVAL_DIR / "rubric.md"
PROMPT_PATH = EVAL_DIR / "prompts" / "judge_v1.txt"
CONFIG_PATH = EVAL_DIR / "config.yaml"
FIXTURES_PATH = EVAL_DIR / "fixtures" / "mock_generations.json"

#: 브리프가 지정한 6개 시나리오(①~⑥)에 대응하는 고정 id 집합.
EXPECTED_GENERATION_IDS = {
    "grounded_concrete",
    "grounded_vague",
    "fabricated_number",
    "fabricated_notice",
    "paraphrase_only",
    "auto_order_directive",
}

#: 브리프 정의상 hallucination=true여야 하는 케이스(③ 수치 날조, ④ 없는 공고 인용)만.
EXPECTED_TRUE_HALLUCINATION_IDS = {"fabricated_number", "fabricated_notice"}


def _load_fixtures() -> dict:
    return json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))


def _fixtures_by_id() -> dict[str, dict]:
    return {gen["id"]: gen for gen in _load_fixtures()["generations"]}


def _load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# eval/ 패키지 레이아웃
# ---------------------------------------------------------------------------


class TestEvalPackageLayout:
    def test_eval_init_exists(self):
        assert (EVAL_DIR / "__init__.py").exists()

    def test_eval_package_is_importable(self):
        import eval as eval_pkg

        assert eval_pkg is not None


# ---------------------------------------------------------------------------
# 목업 픽스처 스키마 검증(6건·expected 완비·구간 유효)
# ---------------------------------------------------------------------------


class TestMockGenerationsFixture:
    def test_fixture_file_exists_and_parses(self):
        assert FIXTURES_PATH.exists()
        data = _load_fixtures()
        assert isinstance(data, dict)

    def test_rubric_version_tag(self):
        data = _load_fixtures()
        assert data.get("rubric_version") == "v1"

    def test_has_shared_case_context(self):
        data = _load_fixtures()
        assert isinstance(data["case_context"], dict)
        assert data["case_context"]  # 비어있지 않음

    def test_exactly_six_generations(self):
        data = _load_fixtures()
        assert len(data["generations"]) == 6

    def test_generation_ids_match_six_scenarios(self):
        """브리프가 지정한 정확히 그 6개 시나리오(①~⑥)가 전부 있어야 한다(단순 개수 6이 아님)."""
        ids = {gen["id"] for gen in _load_fixtures()["generations"]}
        assert ids == EXPECTED_GENERATION_IDS

    def test_each_generation_has_required_fields(self):
        for gen in _load_fixtures()["generations"]:
            assert isinstance(gen["id"], str) and gen["id"]
            assert isinstance(gen["label"], str) and gen["label"]
            assert isinstance(gen["text"], str) and gen["text"].strip()
            assert isinstance(gen["expected"], dict)

    def test_expected_block_has_exactly_four_keys(self):
        for gen in _load_fixtures()["generations"]:
            assert set(gen["expected"].keys()) == {
                "hallucination",
                "groundedness",
                "cause_relevance",
                "actionability",
            }, gen["id"]

    def test_expected_hallucination_is_bool(self):
        for gen in _load_fixtures()["generations"]:
            assert isinstance(gen["expected"]["hallucination"], bool), gen["id"]

    @pytest.mark.parametrize("axis", ["groundedness", "cause_relevance", "actionability"])
    def test_expected_ranges_are_valid_intervals(self, axis):
        for gen in _load_fixtures()["generations"]:
            lo, hi = gen["expected"][axis]
            assert 0.0 <= lo <= hi <= 1.0, f"{gen['id']}.{axis} 구간이 유효하지 않음: [{lo}, {hi}]"

    def test_hallucination_true_only_for_fabrication_cases(self):
        for gen in _load_fixtures()["generations"]:
            expected_flag = gen["id"] in EXPECTED_TRUE_HALLUCINATION_IDS
            assert gen["expected"]["hallucination"] == expected_flag, gen["id"]


# ---------------------------------------------------------------------------
# 목업 판별력 검증(핵심) — 축·케이스 간 기대 구간이 실제로 서로 구분되는지
# ---------------------------------------------------------------------------


class TestFixtureDiscriminativePower:
    """브리프 핵심 요구: 6건이 단순 예시가 아니라 서로 구분되는 판정을 유도해야 한다."""

    def test_concrete_action_scores_higher_than_vague_action(self):
        by_id = _fixtures_by_id()
        concrete_min = by_id["grounded_concrete"]["expected"]["actionability"][0]
        vague_max = by_id["grounded_vague"]["expected"]["actionability"][1]
        assert concrete_min > vague_max

    def test_concrete_action_scores_higher_than_paraphrase_only(self):
        by_id = _fixtures_by_id()
        concrete_min = by_id["grounded_concrete"]["expected"]["actionability"][0]
        paraphrase_max = by_id["paraphrase_only"]["expected"]["actionability"][1]
        assert concrete_min > paraphrase_max

    def test_fabricated_cases_score_lower_groundedness_than_all_grounded_cases(self):
        by_id = _fixtures_by_id()
        fabricated_max = max(
            by_id["fabricated_number"]["expected"]["groundedness"][1],
            by_id["fabricated_notice"]["expected"]["groundedness"][1],
        )
        grounded_ids = EXPECTED_GENERATION_IDS - EXPECTED_TRUE_HALLUCINATION_IDS
        for gid in grounded_ids:
            grounded_min = by_id[gid]["expected"]["groundedness"][0]
            assert grounded_min > fabricated_max, gid

    def test_paraphrase_only_is_not_penalized_as_hallucination(self):
        """경계 케이스(⑤): 근거 재표현만으로는 hallucination이 아니고 groundedness도 높아야 함."""
        paraphrase = _fixtures_by_id()["paraphrase_only"]["expected"]
        assert paraphrase["hallucination"] is False
        assert paraphrase["groundedness"][0] >= 0.85

    def test_auto_order_directive_is_not_hallucination_nor_actionability_penalized(self):
        """명시 원칙(⑥): 자동 발주 권고는 hallucination도 actionability 감점도 아니다."""
        auto_order = _fixtures_by_id()["auto_order_directive"]["expected"]
        assert auto_order["hallucination"] is False
        assert auto_order["actionability"][0] >= 0.6

    def test_fabricated_number_keeps_cause_relevance_despite_low_groundedness(self):
        """③: 수치는 날조했지만 원인 지목 자체는 근거와 맞물려야 한다 — 축 간 독립성 검증."""
        fabricated = _fixtures_by_id()["fabricated_number"]["expected"]
        assert fabricated["groundedness"][1] <= 0.35
        assert fabricated["cause_relevance"][0] >= 0.5


# ---------------------------------------------------------------------------
# eval/config.yaml — 필수 키 및 교차 judge 매핑
# ---------------------------------------------------------------------------


class TestConfigYaml:
    def test_config_file_exists_and_parses(self):
        assert CONFIG_PATH.exists()
        cfg = _load_config()
        assert isinstance(cfg, dict)

    def test_required_top_level_keys(self):
        cfg = _load_config()
        for key in (
            "rubric_version",
            "prompt_version",
            "temperature",
            "judge_by_generation_provider",
            "dataset",
        ):
            assert key in cfg, key

    def test_versions_and_temperature(self):
        cfg = _load_config()
        assert cfg["rubric_version"] == "v1"
        assert cfg["prompt_version"] == "judge_v1"
        assert cfg["temperature"] == 0

    def test_cross_judge_mapping_is_dynamic_cross(self):
        """바인딩 결정: 생성 provider 기준 동적 교차 — anthropic↔openai가 서로를 가리켜야 함."""
        cfg = _load_config()
        mapping = cfg["judge_by_generation_provider"]
        assert set(mapping.keys()) == {"anthropic", "openai"}
        assert mapping["anthropic"]["provider"] == "openai"
        assert mapping["openai"]["provider"] == "anthropic"
        assert mapping["anthropic"]["model"]
        assert mapping["openai"]["model"]

    def test_dataset_section(self):
        cfg = _load_config()
        assert cfg["dataset"]["cases"] == 40
        assert cfg["dataset"]["pilot"] == 4
        assert cfg["dataset"]["content_hash"] is None


# ---------------------------------------------------------------------------
# JudgeScore / JudgeOutput pydantic 계약
# ---------------------------------------------------------------------------


class TestJudgeScoreSchema:
    SAMPLE = dict(
        groundedness=0.9,
        cause_relevance=0.85,
        actionability=0.8,
        hallucination=False,
        rationale="근거의 공급중단 공고와 입고 지연을 정확히 인용했다.",
        judge_model="gpt-5-test-snapshot",
        rubric_version="v1",
    )

    def test_construct_and_roundtrip_dict(self):
        score = JudgeScore(**self.SAMPLE)
        restored = JudgeScore.model_validate(score.model_dump())
        assert restored == score

    def test_roundtrip_json(self):
        score = JudgeScore(**self.SAMPLE)
        restored = JudgeScore.model_validate_json(score.model_dump_json())
        assert restored == score

    @pytest.mark.parametrize("field", ["groundedness", "cause_relevance", "actionability"])
    def test_axis_above_one_rejected(self, field):
        bad = dict(self.SAMPLE)
        bad[field] = 1.5
        with pytest.raises(ValidationError):
            JudgeScore(**bad)

    @pytest.mark.parametrize("field", ["groundedness", "cause_relevance", "actionability"])
    def test_axis_negative_rejected(self, field):
        bad = dict(self.SAMPLE)
        bad[field] = -0.1
        with pytest.raises(ValidationError):
            JudgeScore(**bad)

    def test_missing_required_field_rejected(self):
        incomplete = dict(self.SAMPLE)
        del incomplete["judge_model"]
        with pytest.raises(ValidationError):
            JudgeScore(**incomplete)

    def test_judge_score_fields_minus_executor_filled_equal_judge_output_fields(self):
        """바인딩 계약: judge_model·rubric_version(실행기가 채움)을 제외한 평가 필드는
        judge 프롬프트 출력 스키마(JudgeOutput)와 1:1이어야 한다."""
        judge_score_fields = set(JudgeScore.model_fields)
        assert judge_score_fields - JudgeScore.EXECUTOR_FILLED_FIELDS == set(JudgeOutput.model_fields)

    def test_judge_output_alone_parses_llm_shaped_json(self):
        """judge LLM이 실제로 내야 하는 5필드 JSON이 judge_model·rubric_version 없이도
        JudgeOutput으로 파싱된다(= complete_json(schema=JudgeOutput)에 그대로 쓸 수 있음)."""
        raw = {
            "groundedness": 0.9,
            "cause_relevance": 0.85,
            "actionability": 0.8,
            "hallucination": False,
            "rationale": "근거를 그대로 인용했다.",
        }
        output = JudgeOutput.model_validate(raw)
        assert output.groundedness == 0.9


# ---------------------------------------------------------------------------
# eval/rubric.md 내용
# ---------------------------------------------------------------------------


class TestRubricDocument:
    def test_rubric_file_exists(self):
        assert RUBRIC_PATH.exists()

    def test_rubric_version_header(self):
        text = RUBRIC_PATH.read_text(encoding="utf-8")
        assert "rubric_version: v1" in text

    def test_rubric_covers_four_axes(self):
        text = RUBRIC_PATH.read_text(encoding="utf-8")
        for axis in ("groundedness", "cause_relevance", "actionability", "hallucination"):
            assert axis in text

    def test_rubric_states_no_medical_validity_judgment_principle(self):
        text = RUBRIC_PATH.read_text(encoding="utf-8")
        assert "의학적 타당성" in text

    def test_rubric_states_auto_order_flag_principle(self):
        text = RUBRIC_PATH.read_text(encoding="utf-8")
        assert "자동" in text and "플래그" in text

    def test_rubric_hallucination_table_has_at_least_two_true_and_two_false_examples(self):
        text = RUBRIC_PATH.read_text(encoding="utf-8")
        assert text.count("**true**") >= 2
        assert text.count("**false**") >= 2


# ---------------------------------------------------------------------------
# eval/prompts/judge_v1.txt 렌더
# ---------------------------------------------------------------------------


class TestJudgePromptRendering:
    def test_prompt_file_exists(self):
        assert PROMPT_PATH.exists()

    def test_template_declares_three_placeholders(self):
        template = PROMPT_PATH.read_text(encoding="utf-8")
        for placeholder in ("{rubric}", "{case_context}", "{generation_text}"):
            assert placeholder in template

    def test_render_includes_rubric_full_text_and_generation_text(self):
        template = PROMPT_PATH.read_text(encoding="utf-8")
        rubric_text = RUBRIC_PATH.read_text(encoding="utf-8")
        fixtures = _load_fixtures()
        case_context_text = json.dumps(fixtures["case_context"], ensure_ascii=False, indent=2)
        generation_text = fixtures["generations"][0]["text"]

        rendered = template.format(
            rubric=rubric_text,
            case_context=case_context_text,
            generation_text=generation_text,
        )

        assert rubric_text in rendered
        assert case_context_text in rendered
        assert generation_text in rendered

    def test_render_produces_literal_json_schema_braces(self):
        """템플릿 내 출력 스키마 예시({{...}})가 렌더 후 단일 중괄호로 살아남는지 확인한다
        — 이스케이프가 깨지면 .format() 자체가 KeyError로 실패한다."""
        template = PROMPT_PATH.read_text(encoding="utf-8")
        rendered = template.format(rubric="R", case_context="C", generation_text="G")
        assert '"groundedness": number' in rendered
        assert "{{" not in rendered

    def test_render_missing_variable_raises_keyerror(self):
        template = PROMPT_PATH.read_text(encoding="utf-8")
        with pytest.raises(KeyError):
            template.format(rubric="R", case_context="C")  # generation_text 누락


# ---------------------------------------------------------------------------
# (선택) 실 API 스모크 — 키가 없으면 CI 안전하게 skip(이 환경은 2026-08-21 기준 키 없음)
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM_PREAMBLE = (
    "너는 아래 사용자 메시지의 지시만을 따르는 엄격한 채점자다. 지시된 JSON 스키마와"
    " 정확히 일치하는 JSON 객체 하나만 출력하라."
)


def _render_judge_user_message(case_id: str) -> tuple[str, dict]:
    """목업 케이스 하나를 judge_v1.txt에 렌더링해 (user 메시지, generation dict)를 반환."""
    fixtures = _load_fixtures()
    template = PROMPT_PATH.read_text(encoding="utf-8")
    rubric_text = RUBRIC_PATH.read_text(encoding="utf-8")
    case_context_text = json.dumps(fixtures["case_context"], ensure_ascii=False, indent=2)
    generation = next(g for g in fixtures["generations"] if g["id"] == case_id)

    rendered = template.format(
        rubric=rubric_text,
        case_context=case_context_text,
        generation_text=generation["text"],
    )
    return rendered, generation


@pytest.mark.skipif(
    not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    reason="ANTHROPIC_API_KEY/OPENAI_API_KEY가 없으면 실제 judge 스모크를 건너뛴다",
)
@pytest.mark.parametrize("case_id", ["grounded_concrete", "fabricated_number"])
def test_smoke_real_judge_matches_expected_range(case_id):
    # 캐시를 켜 재실행을 무비용으로 만든다(브리프 지시) — settings.LLM_CACHE_PATH를 격리하지
    # 않으므로 실제 data/llm_cache.db에 저장되고, 같은 case_id 재실행은 캐시 히트로 처리된다.
    from medsupply.llm.cache import build_cache_key
    from medsupply.llm.client import RenderedPrompt, complete_json
    from medsupply.llm.config import load_llm_config

    user_message, generation = _render_judge_user_message(case_id)
    provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "openai"
    cfg = load_llm_config()
    model = cfg.anthropic_model if provider == "anthropic" else cfg.openai_model

    prompt = RenderedPrompt(system=_JUDGE_SYSTEM_PREAMBLE, user=user_message, version="judge_v1")
    cache_key = build_cache_key(
        task="eval_judge_smoke",
        prompt_version="judge_v1",
        model=model,
        schema=JudgeOutput,
        payload={"case_id": case_id},
    )

    result = complete_json(
        "eval_judge_smoke",
        prompt,
        JudgeOutput,
        provider=provider,
        cache_key=cache_key,
    )

    expected = generation["expected"]
    assert result.data.hallucination == expected["hallucination"], result.data.rationale
    for axis in ("groundedness", "cause_relevance", "actionability"):
        lo, hi = expected[axis]
        value = getattr(result.data, axis)
        assert lo <= value <= hi, (
            f"{case_id}.{axis}={value} 기대 구간 [{lo},{hi}] 밖 (rationale: {result.data.rationale})"
        )
