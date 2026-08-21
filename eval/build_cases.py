"""S-26: 평가셋 40건 구성(파일럿 4 + 본 36) — 동결 스냅샷에서 결정적으로 구성한다.

최신 run(``queries.get_latest_runs(conn, 1)``)의 risk_results에서 위험·경고 등급 전건과
주의 등급 일부(item_id 오름차순)를 합쳐 최대 ``TARGET_CASE_COUNT``(40)건을 선정한다
(``select_case_rows``). 40건에 못 미치면(등급 분포 변화) 있는 만큼만 반환한다 — 개수를
억지로 맞추지 않는다(task-S26-brief.md §선정 규칙).

파일럿 ``PILOT_COUNT``(4)건은 선정된 케이스 중 risk_type이 서로 다른 대표(각 유형의 최소
item_id)를 우선 뽑고, 4유형에 못 미치면 케이스 목록 선두(item_id 오름차순)에서 아직 뽑히지
않은 항목으로 채운다(``select_pilot_ids``).

각 케이스는 자기완결이다 — evidence는 ``medsupply.llm.grounding.collect_risk_evidence``
(M-20, LLM 미관여 결정적 코드)가 조립한 RiskEvidence 그대로이고, history는
``medsupply.llm.explanation.explain_item``과 완전히 동일한 규칙(``_trim_history`` 재사용,
중복 구현 금지)으로 최근 3건만 추린다. 표준 DB는 읽기 전용으로만 연다 — 이 모듈 어디에도
INSERT/UPDATE/DELETE가 없다. ground truth 라벨은 참조하지 않는다(collect_risk_evidence·
queries가 다루는 위험 판정 데이터만 쓴다 — 라벨이 아니다).

CLI: ``python -m eval.build_cases --db data/medsupply.db --out eval/cases/eval_cases_v1.json``
케이스 파일을 쓴 뒤 그 파일의 sha256을 ``eval/config.yaml``의 ``dataset.content_hash``에,
실제 케이스·파일럿 수를 ``dataset.cases``/``dataset.pilot``에 반영한다(정규식 치환 — yaml
왕복은 config.yaml 전역의 한국어 설명 주석을 지워버리므로 쓰지 않는다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

from medsupply.data import db, queries
from medsupply.llm.explanation import _HISTORY_LIMIT, _trim_history
from medsupply.llm.grounding import collect_risk_evidence

DATASET_VERSION = "eval_cases_v1"
TARGET_CASE_COUNT = 40
PILOT_COUNT = 4

#: 무조건 전건 포함되는 등급(브리프 §선정 규칙 1) — 개수 상한 없음.
PRIORITY_GRADES = ("위험", "경고")
#: 부족분을 채우는 등급(브리프 §선정 규칙 2) — item_id 오름차순으로 목표치까지만.
FILL_GRADE = "주의"

DEFAULT_OUT_PATH = "eval/cases/eval_cases_v1.json"
DEFAULT_CONFIG_PATH = "eval/config.yaml"


# ---------------------------------------------------------------------------
# 순수 함수 — 선정 규칙(DB 미접근, DataFrame/문자열만)
# ---------------------------------------------------------------------------


def select_case_rows(risk_df: pd.DataFrame) -> pd.DataFrame:
    """등급 우선순위 규칙으로 케이스 대상 행을 item_id(=case_id) 오름차순으로 선정한다.

    위험·경고 등급은 개수와 무관하게 전건 포함한다("전건 포함" — 상한 없음). 나머지는
    TARGET_CASE_COUNT에 도달할 때까지 주의 등급에서 item_id 오름차순으로 채운다. 위험+경고
    +주의 합이 TARGET_CASE_COUNT 미만이면 있는 만큼만 반환한다(수치 조정 금지).
    """
    priority_rows = risk_df[risk_df["grade"].isin(PRIORITY_GRADES)]

    remaining = TARGET_CASE_COUNT - len(priority_rows)
    if remaining > 0:
        fill_rows = risk_df[risk_df["grade"] == FILL_GRADE].sort_values("item_id").head(remaining)
    else:
        fill_rows = risk_df.iloc[0:0]

    selected = pd.concat([priority_rows, fill_rows], ignore_index=True)
    return selected.sort_values("item_id").reset_index(drop=True)


def select_pilot_ids(selected_rows: pd.DataFrame) -> list[str]:
    """선정된 케이스 중 risk_type이 서로 다른 (최대) PILOT_COUNT건의 item_id를 고른다.

    1차: risk_type별 최소 item_id를 대표로 뽑고, 대표들을 item_id 오름차순 정렬해 앞에서
    PILOT_COUNT개만 취한다(유형이 PILOT_COUNT보다 많으면 나머지 대표는 탈락).
    2차(미충족 채움): 대표가 PILOT_COUNT에 못 미치면, 선정 목록 선두(item_id 오름차순)부터
    훑어 아직 파일럿에 없는 item_id를 순서대로 채운다 — "4유형 미충족 시 있는 유형으로
    채우고 부족분은 목록 선두"(중복 유형이라도 무방).
    """
    ordered = selected_rows.sort_values("item_id")

    type_reps: dict[str, str] = {}
    for row in ordered.itertuples(index=False):
        if row.risk_type not in type_reps:
            type_reps[row.risk_type] = row.item_id

    pilot = sorted(type_reps.values())[:PILOT_COUNT]

    if len(pilot) < PILOT_COUNT:
        pilot_set = set(pilot)
        for item_id in ordered["item_id"]:
            if len(pilot) >= PILOT_COUNT:
                break
            if item_id not in pilot_set:
                pilot.append(item_id)
                pilot_set.add(item_id)
        pilot.sort()

    return pilot


def params_hash_from_run_id(run_id: str) -> str | None:
    """run_id(``f"{as_of}#{params_hash[:8]}"``)에서 '#' 뒤 params_hash를 뽑는다.

    '#'이 없는 run_id는 그 자체로는 params_hash 개념이 없으므로 None
    (medsupply.data.queries.get_latest_runs의 "패밀리 없음" 처리와 동일 관례).
    """
    _, sep, family = run_id.partition("#")
    return family if sep else None


# ---------------------------------------------------------------------------
# DB I/O — collect_risk_evidence·list_action_history만 읽는다(읽기 전용, 쓰기 없음)
# ---------------------------------------------------------------------------


def _build_case(conn, run_id: str, item_id: str, *, is_pilot: bool) -> dict:
    """단일 케이스 dict — evidence는 collect_risk_evidence, history는 explain_item과 완전히
    동일한 규칙(evidence.risk_type으로 필터한 최근 이력 + _trim_history)으로 추린다."""
    evidence = collect_risk_evidence(conn, item_id, run_id=run_id)

    history_df = queries.list_action_history(
        conn, item_id=item_id, risk_type=evidence.risk_type, limit=_HISTORY_LIMIT
    )
    history = _trim_history(history_df.to_dict(orient="records"))

    return {
        "case_id": f"EC-{item_id}",
        "item_id": item_id,
        "run_id": run_id,
        "is_pilot": is_pilot,
        "evidence": evidence.model_dump(),
        "history": history,
    }


def build_dataset(conn) -> dict:
    """{"meta": {...}, "cases": [...]} — 브리프 §산출물의 케이스 스키마·메타 필드 그대로."""
    latest_runs = queries.get_latest_runs(conn, 1)
    if not latest_runs:
        raise ValueError("build_cases: risk_results에 run이 전혀 없습니다.")
    run_id = latest_runs[0]

    risk_df = queries.get_risk_results(conn, run_id)
    selected = select_case_rows(risk_df)
    pilot_ids = select_pilot_ids(selected)
    pilot_id_set = set(pilot_ids)

    cases = [
        _build_case(conn, run_id, row.item_id, is_pilot=row.item_id in pilot_id_set)
        for row in selected.itertuples(index=False)
    ]
    cases.sort(key=lambda c: c["case_id"])

    meta_row = queries.get_meta(conn)

    meta = {
        "dataset_version": DATASET_VERSION,
        "built_from_run": run_id,
        "dataset_content_hash": meta_row.get("content_hash"),
        "params_hash": params_hash_from_run_id(run_id),
        "case_count": len(cases),
        "pilot_ids": pilot_ids,
    }
    return {"meta": meta, "cases": cases}


# ---------------------------------------------------------------------------
# 파일 I/O — 케이스 JSON 기록 + eval/config.yaml dataset 섹션 갱신
# ---------------------------------------------------------------------------


def write_cases_file(path: str | Path, dataset: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(dataset, f, ensure_ascii=False, indent=2)
        f.write("\n")


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


_DATASET_CASES_RE = re.compile(r"(?m)^(  cases:)[^\n]*$")
_DATASET_PILOT_RE = re.compile(r"(?m)^(  pilot:)[^\n]*$")
_DATASET_HASH_RE = re.compile(r"(?m)^(  content_hash:)[^\n]*$")


def render_updated_dataset_section(
    config_text: str, *, case_count: int, pilot_count: int, content_hash: str
) -> str:
    """eval/config.yaml의 ``dataset:`` 블록 3개 값만 치환한다(다른 줄·주석은 그대로 보존).

    yaml.safe_load→dump 왕복은 config.yaml 전역에 흩어진 한국어 설명 주석을 지워버리므로
    쓰지 않는다 — 정규식으로 그 3줄만 정확히 치환한다. content_hash는 sha256 hex(전부
    숫자로만 나올 가능성은 희박하지만 0은 아니다)라 YAML이 정수로 오인하지 않도록 항상
    따옴표로 감싼다.
    """
    updated = _DATASET_CASES_RE.sub(rf"\g<1> {case_count}", config_text, count=1)
    updated = _DATASET_PILOT_RE.sub(rf"\g<1> {pilot_count}", updated, count=1)
    updated = _DATASET_HASH_RE.sub(rf'\g<1> "{content_hash}"', updated, count=1)
    return updated


def update_config_yaml(
    config_path: str | Path, *, case_count: int, pilot_count: int, content_hash: str
) -> None:
    path = Path(config_path)
    original = path.read_text(encoding="utf-8")
    updated = render_updated_dataset_section(
        original, case_count=case_count, pilot_count=pilot_count, content_hash=content_hash
    )
    path.write_text(updated, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 평가셋(40건, 파일럿 4건) 동결 스냅샷 구성 CLI"
    )
    parser.add_argument("--db", required=True, help="표준 DB 경로(읽기 전용)")
    parser.add_argument("--out", required=True, help="케이스 JSON 출력 경로")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG_PATH, help=f"갱신할 eval config 경로(기본 {DEFAULT_CONFIG_PATH})"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = db.get_connection(args.db)
    try:
        dataset = build_dataset(conn)
    finally:
        conn.close()

    write_cases_file(args.out, dataset)
    content_hash = sha256_file(args.out)
    update_config_yaml(
        args.config,
        case_count=dataset["meta"]["case_count"],
        pilot_count=len(dataset["meta"]["pilot_ids"]),
        content_hash=content_hash,
    )

    print(
        f"built {dataset['meta']['case_count']} cases"
        f" (pilot={len(dataset['meta']['pilot_ids'])}) from run {dataset['meta']['built_from_run']}"
    )
    print(f"content_hash={content_hash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
