"""S-27: 교차 provider judge 실행기 + check_completeness + run_experiment 골격.

judge_generation은 medsupply.llm.client.complete_json을 provider 강제 인자로 재사용해 케이스
1건의 생성문을 채점한다(마스터 플랜 결정 36·38·48). 교차 규칙(바인딩 결정, 어길 수 없음):
생성 meta.provider가 anthropic이면 judge는 eval/config.yaml의 openai 항목으로, openai면
anthropic 항목으로 — 같은 공급자가 자기 생성물을 채점하지 않는다. temperature는
config.yaml의 값(0)을 그대로 complete_json에 실어 보내되, Anthropic 경로는
medsupply.llm.client의 문서화된 제약(claude-opus-5는 temperature 미지원, 전달 시 400)에
따라 항상 제외된다 — judge_generation 자신은 이 제외를 신경 쓰지 않고 그냥 값을 넘긴다.

check_completeness는 LLM이 전혀 관여하지 않는 결정적 4종 검사(등급·원인·대응·근거)다.

run_experiment는 케이스셋(S-26, eval/cases/eval_cases_v1.json)을 순회하며 explain(캐시 재사용,
DB 불요 — evidence는 케이스에 동봉된 것을 그대로 쓴다) → judge_generation → check_completeness
→ eval/results/{name}.jsonl 기록 → 요약 dict를 만든다. 이 태스크의 구현·테스트는 전부
모킹이다(키 불요) — S-28이 실제로 40건을 돌린다.

표준 DB는 이 모듈 어디에서도 열지 않는다(격리 — "케이스가 자기완결"이라는 S-26의 설계를
그대로 신뢰한다).
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

import yaml

from medsupply.llm.cache import build_cache_key
from medsupply.llm.client import RenderedPrompt, complete_json
from medsupply.llm.explanation import generate_risk_explanation
from medsupply.llm.schemas import RiskEvidence
from medsupply.llm.tracing import observed

from eval.schemas import JudgeOutput, JudgeScore

#: eval/judge.py 자신의 위치 기준 — 패키지 내부 고정 리소스(rubric.md·judge_v1.txt)는
#: 호출부의 CWD와 무관하게 항상 찾아야 하므로 medsupply/data/db.py의 SCHEMA_PATH 관례를
#: 그대로 따른다(__file__ 기준 절대 경로).
_EVAL_DIR = Path(__file__).resolve().parent
_RUBRIC_PATH = _EVAL_DIR / "rubric.md"
_JUDGE_PROMPT_PATH = _EVAL_DIR / "prompts" / "judge_v1.txt"

#: config_path·dataset_path는 반대로 브리프가 명시한 그대로 CWD 상대 경로 기본값이다
#: (build_cases.py의 CLI 인자·`python -m eval.build_cases` 실행 관례와 동일 — 저장소 루트
#: 에서 실행한다는 전제).
DEFAULT_CONFIG_PATH = "eval/config.yaml"
DEFAULT_DATASET_PATH = "eval/cases/eval_cases_v1.json"
#: run_experiment의 jsonl 출력 디렉터리(모듈 속성 — 테스트가 tmp_path로 monkeypatch해
#: 격리한다. medsupply.settings.LLM_CACHE_PATH를 tests/llm/conftest.py가 격리하는 것과
#: 동일한 관례).
RESULTS_DIR = Path("eval/results")

_JUDGE_TASK = "judge"

#: judge_v1.txt 자체가 이미 채점 지시·출력 스키마를 전부 담고 있어(user 메시지), system은
#: "그 지시만 따르라"는 최소 제약만 둔다(tests/eval/test_rubric_fixtures.py의 실 API 스모크가
#: 쓰던 문구와 동일한 취지 — 이 파일은 그 테스트 모듈을 import하지 않고 독립적으로 정의한다).
SYSTEM_PROMPT = (
    "너는 아래 사용자 메시지의 지시만을 따르는 엄격한 채점자다. 지정된 JSON 스키마와"
    " 정확히 일치하는 JSON 객체 하나만 출력한다."
)


# ---------------------------------------------------------------------------
# judge_generation — 케이스 1건 채점
# ---------------------------------------------------------------------------


def _load_judge_config(config_path: str | Path) -> dict:
    """config_path를 읽어 파싱한다. 파일이 없으면 FileNotFoundError가 그대로 전파된다."""
    return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))


def _resolve_judge_binding(cfg: dict, generation_provider: str) -> tuple[str, str]:
    """교차 규칙(생성 provider 기준 동적 교차, 바인딩 결정) — (judge_provider, judge_model).

    judge_model은 config에 적힌 값을 그대로 반환한다(별칭+TODO 상태 — task-S18-brief.md가
    허용, ID 확정은 별도 작업). 이 값은 cache_key 조립에만 쓰이고, JudgeScore.judge_model은
    이후 complete_json이 실제로 반환한 모델 문자열로 별도로 채워진다.
    """
    mapping = cfg.get("judge_by_generation_provider")
    if not mapping or generation_provider not in mapping:
        raise ValueError(
            "judge_generation: judge_by_generation_provider에 생성 provider"
            f" {generation_provider!r} 항목이 없습니다(가용: {sorted((mapping or {}).keys())})."
        )
    entry = mapping[generation_provider]
    if not isinstance(entry, dict) or "provider" not in entry or "model" not in entry:
        raise ValueError(
            f"judge_generation: judge_by_generation_provider[{generation_provider!r}]에"
            f" provider/model 키가 모두 있어야 합니다(실값: {entry!r})."
        )
    return entry["provider"], entry["model"]


@observed("judge")
def judge_generation(
    case: dict, generation: dict, *, config_path: str | Path = DEFAULT_CONFIG_PATH
) -> JudgeScore:
    """케이스 1건 + 생성물 1건을 교차 provider judge로 채점한다.

    Args:
        case: eval_cases_v1.json의 케이스 1건(case_id·evidence 등, S-26 스키마 그대로).
        generation: {"explanation": RiskExplanation.model_dump(), "meta": {provider, model,
            prompt_version}} — explain 단계(generate_risk_explanation)의 결과를 그대로 감싼
            형태.
        config_path: rubric_version·prompt_version·judge_by_generation_provider를 읽어올
            eval config 경로. 없으면 FileNotFoundError, 생성 provider에 대응하는 교차 항목이
            없거나 불완전하면 ValueError(both "config 미존재·항목 결손 에러" — 두 경우 모두
            LLM을 호출하기 전에 실패한다).

    Returns:
        JudgeScore — judge_model(실제로 호출된 모델, 별칭이 아님)·rubric_version(config)을
        실행기가 채운 완성형.
    """
    cfg = _load_judge_config(config_path)
    rubric_version = cfg["rubric_version"]
    prompt_version = cfg["prompt_version"]
    temperature = cfg.get("temperature")

    generation_provider = generation["meta"]["provider"]
    judge_provider, judge_model_alias = _resolve_judge_binding(cfg, generation_provider)

    rubric_text = _RUBRIC_PATH.read_text(encoding="utf-8")
    template = _JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    case_context_text = json.dumps(case["evidence"], ensure_ascii=False, indent=2)
    generation_text = json.dumps(generation["explanation"], ensure_ascii=False, indent=2)

    user_message = template.format(
        rubric=rubric_text, case_context=case_context_text, generation_text=generation_text
    )
    prompt = RenderedPrompt(system=SYSTEM_PROMPT, user=user_message, version=prompt_version)

    cache_key = build_cache_key(
        _JUDGE_TASK,
        rubric_version,
        judge_model_alias,
        JudgeOutput,
        {"case_id": case["case_id"], "generation": generation, "rubric_version": rubric_version},
    )

    result = complete_json(
        _JUDGE_TASK,
        prompt,
        JudgeOutput,
        provider=judge_provider,
        temperature=temperature,
        cache_key=cache_key,
    )

    return JudgeScore(
        **result.data.model_dump(),
        judge_model=result.model,
        rubric_version=rubric_version,
    )


# ---------------------------------------------------------------------------
# check_completeness — 결정적 4종 완결성 검사(LLM 미관여)
# ---------------------------------------------------------------------------


def check_completeness(risk_row: dict, explanation: dict) -> list[str]:
    """위험 판정 행 + 생성문(dict)의 완결성 4종을 검사한다. 위반 항목 문자열 리스트(빈
    리스트 = 완결).

    1. 등급 존재: risk_row.grade가 비어있지 않음.
    2. 원인 설명 비어있지 않음: explanation.cause_summary(공백만 있는 값도 결손으로 본다).
    3. 대응 ≥1: explanation.actions.
    4. 근거 존재: evidence_refs ≥1 — 전체(explanation.evidence_refs)와 action 단위(각
       action.evidence_refs)를 각각 독립적으로 지적한다(medsupply.llm.grounding.
       verify_explanation_grounding의 empty_refs 검사와 동일한 2단 구조 — 전체가 비어 있어도
       특정 action만 비어 있어도 둘 다 결손이다).
    """
    violations: list[str] = []

    if not risk_row.get("grade"):
        violations.append("missing_grade")

    cause_summary = explanation.get("cause_summary")
    if not cause_summary or not str(cause_summary).strip():
        violations.append("missing_cause_summary")

    actions = explanation.get("actions") or []
    if len(actions) == 0:
        violations.append("missing_actions")

    if not explanation.get("evidence_refs"):
        violations.append("missing_evidence_refs")
    for index, action in enumerate(actions):
        action = action or {}
        if not action.get("evidence_refs"):
            title = action.get("title", "")
            violations.append(f"missing_evidence_refs: actions[{index}] ({title!r})")

    return violations


# ---------------------------------------------------------------------------
# run_experiment — 케이스셋 순회 골격(본실행은 S-28)
# ---------------------------------------------------------------------------


def _run_single_case(case: dict, *, prompt_version: str, force_refresh: bool) -> dict:
    """케이스 1건: explain(evidence는 케이스에 동봉된 것) → judge → completeness."""
    evidence = RiskEvidence.model_validate(case["evidence"])
    explanation_result = generate_risk_explanation(
        evidence,
        history=case.get("history", ()),
        prompt_version=prompt_version,
        force_refresh=force_refresh,
    )
    explanation_dump = explanation_result.explanation.model_dump()

    generation = {
        "explanation": explanation_dump,
        "meta": {
            "provider": explanation_result.provider,
            "model": explanation_result.model,
            "prompt_version": explanation_result.prompt_version,
        },
    }
    score = judge_generation(case, generation)
    violations = check_completeness(case["evidence"], explanation_dump)

    return {
        "case_id": case["case_id"],
        "scores": score.model_dump(),
        "flags": list(explanation_result.hallucination_flags),
        "completeness": violations,
    }


def _summarize(records: list[dict], case_count: int) -> dict:
    """judged 건에 대해서만 평균·비율을 낸다(실패 건은 분모에서 제외) — judged==0이면
    hallucination_rate·mean_*는 None(0으로 위장하지 않는다, measure_mape.py와 동일 관례)."""
    judged = len(records)
    if judged == 0:
        return {
            "cases": case_count,
            "judged": 0,
            "hallucination_rate": None,
            "mean_groundedness": None,
            "mean_cause_relevance": None,
            "mean_actionability": None,
            "completeness_violations": 0,
        }

    hallucination_count = sum(1 for r in records if r["scores"]["hallucination"])
    violation_count = sum(len(r["completeness"]) for r in records)

    return {
        "cases": case_count,
        "judged": judged,
        "hallucination_rate": round(hallucination_count / judged, 4),
        "mean_groundedness": round(statistics.mean(r["scores"]["groundedness"] for r in records), 4),
        "mean_cause_relevance": round(statistics.mean(r["scores"]["cause_relevance"] for r in records), 4),
        "mean_actionability": round(statistics.mean(r["scores"]["actionability"] for r in records), 4),
        "completeness_violations": violation_count,
    }


def run_experiment(
    name: str,
    *,
    prompt_version: str,
    dataset_path: str | Path = DEFAULT_DATASET_PATH,
    limit: int | None = None,
    force_refresh: bool = False,
) -> dict:
    """케이스셋(dataset_path)을 순회해 eval/results/{name}.jsonl을 기록하고 요약 dict를
    반환한다.

    limit은 파일럿(4건) 등 소규모 실행용 — dataset의 케이스 목록 선두 limit개만 처리한다
    (S-26의 is_pilot 플래그와는 별개다 — 특정 파일럿 케이스만 골라 돌리고 싶다면 호출부가
    dataset을 직접 필터링해 별도 파일로 넘기면 된다).

    실패 건은 격리한다 — 케이스 1건에서 예외가 나도 배치 전체가 멈추지 않는다. 실패한
    케이스도 jsonl에 1줄 기록되지만(scores/flags/completeness는 None, "error" 키가 추가된다)
    성공 건과 달리 요약(judged·hallucination_rate·mean_*)에는 집계되지 않는다.

    표준 DB는 열지 않는다 — evidence는 케이스에 동봉된 것을 RiskEvidence로 그대로 복원해
    쓴다.
    """
    dataset = json.loads(Path(dataset_path).read_text(encoding="utf-8"))
    cases = dataset["cases"]
    if limit is not None:
        cases = cases[:limit]

    results_path = RESULTS_DIR / f"{name}.jsonl"
    results_path.parent.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    with results_path.open("w", encoding="utf-8") as f:
        for case in cases:
            try:
                record = _run_single_case(case, prompt_version=prompt_version, force_refresh=force_refresh)
            except Exception as exc:  # noqa: BLE001 - 케이스 1건 실패가 배치 전체를 막지 않는다(격리).
                record = {
                    "case_id": case.get("case_id"),
                    "scores": None,
                    "flags": None,
                    "completeness": None,
                    "error": repr(exc),
                }
            else:
                records.append(record)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return _summarize(records, len(cases))
