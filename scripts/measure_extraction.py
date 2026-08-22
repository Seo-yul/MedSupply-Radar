"""CLI 진입점 — 공고 추출 정확도 측정(골드 대조, Task S-25).

notice_extractions(LLM 추출분)을 골드라벨(data/notices/gold/gold_labels_v1.json, Task S-24)과
대조해 필드별 정확도와 needs_review(확인 필요 상태) 적중을 측정한다.

**격리 원칙**: 이 스크립트는 tests/test_isolation.py의 GOLD_LABELS_PATH_ALLOWLIST가 이미
문서화한 대로(S-24) scripts/measure_detection.py와 동급으로 격리 정적 스캔에서 전면
제외된다 — SCRIPTS_PATH_TARGETS에 등록하지 않는다(등록하면 오히려 "data/notices/gold"
리터럴 사용이 exemptions 없이 위반으로 잡힌다). 골드 라벨을 대조하는 측정기가 정답
경로를 읽는 것은 이 스크립트 본연의 역할이다.

사용법:
    python scripts/measure_extraction.py --db data/medsupply.db \\
        --gold data/notices/gold/gold_labels_v1.json \\
        --out reports/llm/extraction_accuracy.json

## 0행 사전조건(현재 스냅샷의 기본 경로)
notice_extractions가 통째로 0행이면(process_notices.py를 아직 한 번도 실행하지 않은
상태) "추출 미실행" 안내를 stderr에 찍고 exit 1 — 출력 파일은 쓰지 않는다. 이 검사는
골드 파일을 열기 전에(연결 직후) 수행한다 — 어차피 비교할 추출이 하나도 없으므로 골드
파싱 자체가 무의미하기 때문이다. 개별 골드 notice_id가 (표 전체는 비어있지 않지만) 그
notice_id만 추출이 없는 경우는 에러가 아니라 정상적인 "unextracted" 집계 대상이다
(아래 참조) — 이 두 상황을 혼동하지 않는다.

## 측정 정의(task-S25-brief.md §측정 정의가 산식의 법전)
- 모집단: 골드 notice_id 전건 × notice_extractions(없는 건은 unextracted). 필드별
  정확도·needs_review 재현율/정밀도는 **추출이 있는(extracted) 건만**을 분모로 삼는다
  — 추출값 자체가 없는 notice는 "그 필드가 틀렸다"고 말할 대상(예측값)이 없으므로
  자동 오답으로 세지 않는다(단순 채점 불가 제외). extracted/unextracted 건수는 최상위에
  별도로 병기해 커버리지를 드러낸다.
- notice_type: exact 일치율.
- halt_start_date: null 포함 exact 일치율(예외 없음).
- expected_restart_date: null 포함 exact 일치율. 단 N-001·N-014·N-017은 골드 notes에
  "재개일자/정상화 예상일자 이중 표기"가 있어(S-24 리뷰 권고) 두 값 중 어느 쪽이든
  정답 인정한다 — notes 자유 서술을 파싱하지 않고
  ALTERNATE_EXPECTED_RESTART_DATES 허용 목록 상수로 명시한다(notice_id별로 정확히
  그 대안 값만 인정 — 임의의 다른 날짜까지 허용하는 게 아니다).

## strict 뷰(Task X-3 체인 리뷰 F1 — 양가 목록 미적용 대조)
위 "관용(lenient)" 대조는 N-001·N-014·N-017 3건에서 ALTERNATE_EXPECTED_RESTART_DATES를
정답으로 인정한다. 이 허용이 최종 macro_accuracy·needs_review 수치를 얼마나 밀어올리는지
**숨기지 않고 같은 파일에 병기**하기 위해, 결과 최상위에 `strict` 블록을 추가한다 —
양가 목록을 전혀 적용하지 않고(정확히 `gold_value == extracted_value`만) 다시 계산한
{expected_restart_date(accuracy·matched·total), macro_accuracy, needs_review(recall·
precision·tp·fn·fp·tn·misses·false_alarms)}다. expected_restart_date 외 다른 필드는
애초에 양가 허용이 없어 관용/strict가 항상 같으므로 strict 블록에 다시 넣지 않는다(관용
쪽 `per_field`를 그대로 재사용). **기존 키·산식은 전혀 바뀌지 않는다** — `strict`는 최상위에
추가되는 키 하나일 뿐이고, 관용 뷰(`per_field`·`macro_accuracy`·`needs_review`)는 종전과
100% 동일하게 계산된다.
- product_names·ingredient_names: casefold+공백 전체 제거로 정규화한 집합의 완전
  일치율(exact_match_rate, macro_accuracy에 들어가는 "정확도")과 자카드 평균
  (jaccard_mean, 참고 병기 — 자카드는 연속값 유사도라 "정확도"가 아니므로 macro
  평균에서는 제외한다)을 함께 낸다.
- reason: 정확도 산정에서 제외한다(자유 서술). 골드 reason을 casefold 후 공백
  분할한 토큰 중 1개 이상이 추출 reason(casefold)에 부분 문자열로 포함되면 그
  건은 "겹침"으로 세고, 그 비율만 reason_overlap_rate로 참고 병기한다.
- needs_review: "골드 스코어링 대상 필드(notice_type·halt_start_date·
  expected_restart_date·product_names·ingredient_names) 중 1개 이상 불일치"인 건을
  양성으로 두고, 추출 status == '확인 필요'를 양성 예측으로 삼아 재현율·정밀도를 낸다.
  자동확정/확인 완료인데 불일치(양성을 negative로 예측) = 미탐(misses), 확인
  필요인데 전 필드 일치(negative를 양성으로 예측) = 과탐(false_alarms) — 각 건 목록을
  notice_id·status(+미탐은 mismatched_fields)와 함께 싣는다. 분모가 0이면(예: 추출
  0건) recall·precision 모두 null.
- macro_accuracy: notice_type·halt_start_date·expected_restart_date의 accuracy +
  product_names·ingredient_names의 exact_match_rate, 5개의 단순 평균(참고 지표인
  reason_overlap_rate·jaccard_mean은 제외). 5개 중 하나라도 null(추출 0건)이면 null.
- per_notice: 골드 notice_id 전건(정렬)에 대해 {notice_id, mismatched_fields}.
  mismatched_fields는 SCORED_FIELDS 순서를 따르는 불일치 필드명 리스트이며, 그
  notice가 unextracted면 값 대신 센티널 ["unextracted"] 하나만 담는다(필드명이 아니라
  "비교 자체가 불가능했다"는 표시 — extracted_count/unextracted_count와 함께 읽는다).

## 결정성
동일 입력(같은 DB·골드 파일) -> 동일 출력(measured_at 제외). notice_id는 항상 정렬해
순회하므로 DB 행 반환 순서·JSON 딕셔너리 순서에 좌우되지 않는다. now() 호출은
measured_at 스탬프에만 쓴다.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# (scripts/measure_mape.py·scripts/measure_detection.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.data import db, queries  # noqa: E402

#: 정확도·needs_review 산정에 실제로 쓰이는 필드(reason은 참고 지표만이라 제외).
#: per_notice.mismatched_fields·needs_review 양성 판정이 모두 이 순서를 따른다.
SCORED_FIELDS: tuple[str, ...] = (
    "notice_type", "halt_start_date", "expected_restart_date",
    "product_names", "ingredient_names",
)

#: N-001·N-014·N-017 — 골드 notes에 "재개일자/정상화 예상일자 이중 표기"가 있어 두 값 중
#: 어느 쪽이든 expected_restart_date 정답으로 인정한다(S-24 리뷰 권고,
#: task-S25-brief.md §측정 정의). notes 필드는 자유 서술이라 파싱하지 않고 이 허용 목록
#: 상수로 고정한다 — 값은 gold_labels_v1.json 원문 대조로 확정.
ALTERNATE_EXPECTED_RESTART_DATES: dict[str, tuple[str, ...]] = {
    # notes: "공급정상화 예상일자(11-05)와 공급재개일자(11-20)가 상이 — 공급재개일자를 채택."
    "N-001": ("2024-11-05",),
    # notes: "공급정상화 예상일자(2025-05-12)와 공급재개일자(2025-08-11)가 3개월 이상 상이."
    "N-014": ("2025-05-12",),
    # notes: "공급재개일자(01-20)가 공급정상화 예상일자·사유 상세 서술(둘 다 01-30)과 10일 차이."
    "N-017": ("2026-01-30",),
}

#: notice_extractions가 통째로 0행일 때 stderr에 찍는 안내(브리프 §CLI 문구 그대로).
_EMPTY_EXTRACTIONS_HINT = "추출 미실행 — scripts/process_notices.py --all 후 재실행"


# ---------------------------------------------------------------------------
# 순수 함수 — 정규화 + 필드 비교(DB·파일 I/O 없음)
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_name(name: str) -> str:
    """casefold + 공백(선두/말미뿐 아니라 내부 포함 전체) 제거."""
    return _WHITESPACE_RE.sub("", name.casefold())


def normalize_name_set(names: list[str]) -> set[str]:
    return {normalize_name(n) for n in names}


def jaccard(a: set[str], b: set[str]) -> float:
    """|교집합|/|합집합|. 양쪽 다 공집합이면 관례상 1.0(완전 일치로 취급)."""
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def expected_restart_date_matches(
    notice_id: str, gold_value: str | None, extracted_value: str | None
) -> bool:
    """expected_restart_date 비교 — 정확히 일치하거나, notice_id가
    ALTERNATE_EXPECTED_RESTART_DATES에 등록돼 있고 extracted_value가 그 허용 목록 중
    하나면 정답으로 인정한다(임의의 다른 값까지 허용하지는 않는다)."""
    if gold_value == extracted_value:
        return True
    alternates = ALTERNATE_EXPECTED_RESTART_DATES.get(notice_id, ())
    return extracted_value in alternates


def reason_tokens(reason: str) -> list[str]:
    """casefold 후 공백 분할한 토큰 리스트(참고 지표 reason_overlap_rate 전용)."""
    return reason.casefold().split()


def reason_overlap(gold_reason: str, extracted_reason: str) -> bool:
    """골드 reason의 정규화 토큰 중 1개 이상이 추출 reason(casefold)에 부분 문자열로
    포함되는지. 추출 reason이 비어 있으면(추출 자체가 없거나 빈 문자열) False."""
    haystack = (extracted_reason or "").casefold()
    if not haystack:
        return False
    return any(token in haystack for token in reason_tokens(gold_reason))


# ---------------------------------------------------------------------------
# 건별 비교 — 필드 일치 여부 + 자카드 + reason 겹침을 한 번에 묶는다
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NoticeComparison:
    notice_id: str
    field_matches: dict[str, bool]  # SCORED_FIELDS 각각의 일치 여부(관용 — 양가 목록 적용)
    expected_restart_date_strict_match: bool  # 양가 목록 미적용, gold_value == extracted_value만
    product_jaccard: float
    ingredient_jaccard: float
    reason_overlap: bool

    @property
    def mismatched_fields(self) -> list[str]:
        return [f for f in SCORED_FIELDS if not self.field_matches[f]]

    @property
    def has_mismatch(self) -> bool:
        return len(self.mismatched_fields) > 0

    @property
    def strict_field_matches(self) -> dict[str, bool]:
        """엄격 판정 — expected_restart_date만 양가 목록 미적용으로 교체하고 나머지
        필드는 관용 판정과 동일하다(애초에 양가 허용이 없는 필드라 달라질 이유가 없다)."""
        return {**self.field_matches, "expected_restart_date": self.expected_restart_date_strict_match}

    @property
    def strict_mismatched_fields(self) -> list[str]:
        return [f for f in SCORED_FIELDS if not self.strict_field_matches[f]]

    @property
    def strict_has_mismatch(self) -> bool:
        return len(self.strict_mismatched_fields) > 0


def compare_notice(notice_id: str, gold: dict, payload: dict) -> NoticeComparison:
    """gold(골드 라벨 1건) vs payload(notice_extractions.payload_json 파싱 결과 1건)."""
    gold_products = normalize_name_set(gold["product_names"])
    extracted_products = normalize_name_set(payload.get("product_names") or [])
    gold_ingredients = normalize_name_set(gold["ingredient_names"])
    extracted_ingredients = normalize_name_set(payload.get("ingredient_names") or [])
    gold_restart = gold["expected_restart_date"]
    extracted_restart = payload.get("expected_restart_date")

    field_matches = {
        "notice_type": gold["notice_type"] == payload.get("notice_type"),
        "halt_start_date": gold["halt_start_date"] == payload.get("halt_start_date"),
        "expected_restart_date": expected_restart_date_matches(
            notice_id, gold_restart, extracted_restart
        ),
        "product_names": gold_products == extracted_products,
        "ingredient_names": gold_ingredients == extracted_ingredients,
    }

    return NoticeComparison(
        notice_id=notice_id,
        field_matches=field_matches,
        expected_restart_date_strict_match=(gold_restart == extracted_restart),
        product_jaccard=jaccard(gold_products, extracted_products),
        ingredient_jaccard=jaccard(gold_ingredients, extracted_ingredients),
        reason_overlap=reason_overlap(gold["reason"], payload.get("reason") or ""),
    )


# ---------------------------------------------------------------------------
# 집계 — build_report(순수 함수, DB 미개입 — CLI 스모크와 별개로 손검산 테스트 가능)
# ---------------------------------------------------------------------------


def _rate(matched: int, total: int) -> float | None:
    return round(matched / total, 4) if total else None


def _mean_or_none(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def build_report(gold_labels: dict[str, dict], extractions: dict[str, dict]) -> dict:
    """extractions: notice_id -> {"payload": dict, "status": str}(있는 건만 — 없는 골드
    notice_id는 unextracted). 반환 dict는 최종 출력 payload의 measured_at/db/gold_version/
    dataset_content_hash를 제외한 나머지 전 키를 담는다(호출부가 그 4개만 덧붙인다)."""
    notice_ids = sorted(gold_labels)

    comparisons: dict[str, NoticeComparison] = {}
    statuses: dict[str, str] = {}
    unextracted_ids: list[str] = []
    for notice_id in notice_ids:
        row = extractions.get(notice_id)
        if row is None:
            unextracted_ids.append(notice_id)
            continue
        comparisons[notice_id] = compare_notice(notice_id, gold_labels[notice_id], row["payload"])
        statuses[notice_id] = row["status"]

    extracted_ids = [nid for nid in notice_ids if nid in comparisons]
    extracted_count = len(extracted_ids)
    unextracted_count = len(unextracted_ids)

    per_field: dict[str, dict] = {}
    for field_name in ("notice_type", "halt_start_date", "expected_restart_date"):
        matched = sum(1 for nid in extracted_ids if comparisons[nid].field_matches[field_name])
        per_field[field_name] = {
            "accuracy": _rate(matched, extracted_count),
            "matched": matched,
            "total": extracted_count,
        }

    for field_name, jaccard_attr in (
        ("product_names", "product_jaccard"),
        ("ingredient_names", "ingredient_jaccard"),
    ):
        matched = sum(1 for nid in extracted_ids if comparisons[nid].field_matches[field_name])
        jaccards = [getattr(comparisons[nid], jaccard_attr) for nid in extracted_ids]
        per_field[field_name] = {
            "exact_match_rate": _rate(matched, extracted_count),
            "jaccard_mean": _mean_or_none(jaccards),
            "matched": matched,
            "total": extracted_count,
        }

    overlap_matched = sum(1 for nid in extracted_ids if comparisons[nid].reason_overlap)
    per_field["reason"] = {
        "reason_overlap_rate": _rate(overlap_matched, extracted_count),
        "matched": overlap_matched,
        "total": extracted_count,
    }

    macro_components = [
        per_field["notice_type"]["accuracy"],
        per_field["halt_start_date"]["accuracy"],
        per_field["expected_restart_date"]["accuracy"],
        per_field["product_names"]["exact_match_rate"],
        per_field["ingredient_names"]["exact_match_rate"],
    ]
    macro_accuracy = (
        round(statistics.mean(macro_components), 4)
        if all(v is not None for v in macro_components)
        else None
    )

    tp = fn = fp = tn = 0
    misses: list[dict] = []
    false_alarms: list[dict] = []
    for nid in extracted_ids:
        gold_positive = comparisons[nid].has_mismatch
        predicted_positive = statuses[nid] == "확인 필요"
        if gold_positive and predicted_positive:
            tp += 1
        elif gold_positive and not predicted_positive:
            fn += 1
            misses.append(
                {
                    "notice_id": nid,
                    "status": statuses[nid],
                    "mismatched_fields": comparisons[nid].mismatched_fields,
                }
            )
        elif not gold_positive and predicted_positive:
            fp += 1
            false_alarms.append({"notice_id": nid, "status": statuses[nid]})
        else:
            tn += 1

    needs_review = {
        "recall": _rate(tp, tp + fn),
        "precision": _rate(tp, tp + fp),
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "misses": misses,
        "false_alarms": false_alarms,
    }

    # -----------------------------------------------------------------------
    # strict 뷰(Task X-3 체인 리뷰 F1) — ALTERNATE_EXPECTED_RESTART_DATES 양가 목록을
    # 전혀 적용하지 않고 다시 계산한다. expected_restart_date 외 필드는 애초에 양가
    # 허용이 없어 관용과 항상 같으므로 위 per_field를 그대로 재사용한다(재계산 없음).
    # -----------------------------------------------------------------------
    strict_matched = sum(
        1 for nid in extracted_ids if comparisons[nid].expected_restart_date_strict_match
    )
    strict_expected_restart_date = {
        "accuracy": _rate(strict_matched, extracted_count),
        "matched": strict_matched,
        "total": extracted_count,
    }

    strict_macro_components = [
        per_field["notice_type"]["accuracy"],
        per_field["halt_start_date"]["accuracy"],
        strict_expected_restart_date["accuracy"],
        per_field["product_names"]["exact_match_rate"],
        per_field["ingredient_names"]["exact_match_rate"],
    ]
    strict_macro_accuracy = (
        round(statistics.mean(strict_macro_components), 4)
        if all(v is not None for v in strict_macro_components)
        else None
    )

    strict_tp = strict_fn = strict_fp = strict_tn = 0
    strict_misses: list[dict] = []
    strict_false_alarms: list[dict] = []
    for nid in extracted_ids:
        gold_positive = comparisons[nid].strict_has_mismatch
        predicted_positive = statuses[nid] == "확인 필요"
        if gold_positive and predicted_positive:
            strict_tp += 1
        elif gold_positive and not predicted_positive:
            strict_fn += 1
            strict_misses.append(
                {
                    "notice_id": nid,
                    "status": statuses[nid],
                    "mismatched_fields": comparisons[nid].strict_mismatched_fields,
                }
            )
        elif not gold_positive and predicted_positive:
            strict_fp += 1
            strict_false_alarms.append({"notice_id": nid, "status": statuses[nid]})
        else:
            strict_tn += 1

    strict = {
        "expected_restart_date": strict_expected_restart_date,
        "macro_accuracy": strict_macro_accuracy,
        "needs_review": {
            "recall": _rate(strict_tp, strict_tp + strict_fn),
            "precision": _rate(strict_tp, strict_tp + strict_fp),
            "tp": strict_tp, "fn": strict_fn, "fp": strict_fp, "tn": strict_tn,
            "misses": strict_misses,
            "false_alarms": strict_false_alarms,
        },
    }

    per_notice = [
        {
            "notice_id": nid,
            "mismatched_fields": (
                comparisons[nid].mismatched_fields if nid in comparisons else ["unextracted"]
            ),
        }
        for nid in notice_ids
    ]

    return {
        "extracted_count": extracted_count,
        "unextracted_count": unextracted_count,
        "per_field": per_field,
        "needs_review": needs_review,
        "macro_accuracy": macro_accuracy,
        "strict": strict,
        "per_notice": per_notice,
    }


# ---------------------------------------------------------------------------
# DB I/O — notice_extractions만 읽는다(읽기 전용)
# ---------------------------------------------------------------------------


def count_all_extractions(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM notice_extractions").fetchone()[0]


def load_extractions(conn, notice_ids: list[str]) -> dict[str, dict]:
    """notice_ids(골드 전건) 중 notice_extractions에 실제로 있는 행만 dict로 반환한다
    (없는 notice_id는 결과에서 그냥 빠진다 — build_report가 unextracted로 집계)."""
    if not notice_ids:
        return {}
    placeholders = ",".join("?" for _ in notice_ids)
    rows = conn.execute(
        f"SELECT notice_id, payload_json, status FROM notice_extractions"
        f" WHERE notice_id IN ({placeholders})",
        notice_ids,
    ).fetchall()
    return {
        row["notice_id"]: {"payload": json.loads(row["payload_json"]), "status": row["status"]}
        for row in rows
    }


# ---------------------------------------------------------------------------
# 파일 I/O·사람이 읽는 stdout 요약
# ---------------------------------------------------------------------------


def _write_json(path: str | Path, data: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _format_rate(value: float | None) -> str:
    return f"{value:.1%}" if value is not None else "n/a"


def _human_summary(payload: dict) -> str:
    lines = [
        f"추출 {payload['extracted_count']}건 / 미추출 {payload['unextracted_count']}건"
        f" (골드 {payload['extracted_count'] + payload['unextracted_count']}건)",
    ]
    for field_name, field_stats in payload["per_field"].items():
        rate_key = next(
            k for k in ("accuracy", "exact_match_rate", "reason_overlap_rate") if k in field_stats
        )
        line = f"  {field_name}: {rate_key}={_format_rate(field_stats[rate_key])}"
        if "jaccard_mean" in field_stats:
            jaccard_mean = field_stats["jaccard_mean"]
            line += f" jaccard_mean={jaccard_mean if jaccard_mean is not None else 'n/a'}"
        lines.append(line)
    nr = payload["needs_review"]
    lines.append(
        f"needs_review: recall={_format_rate(nr['recall'])} precision={_format_rate(nr['precision'])}"
        f" (tp={nr['tp']} fn={nr['fn']} fp={nr['fp']} tn={nr['tn']})"
    )
    macro = payload["macro_accuracy"]
    lines.append(f"macro_accuracy: {_format_rate(macro)}")

    strict = payload["strict"]
    strict_erd = strict["expected_restart_date"]
    strict_nr = strict["needs_review"]
    lines.append(
        "strict(양가 목록 미적용): expected_restart_date="
        f"{_format_rate(strict_erd['accuracy'])}({strict_erd['matched']}/{strict_erd['total']})"
        f" macro_accuracy={_format_rate(strict['macro_accuracy'])}"
        f" needs_review recall={_format_rate(strict_nr['recall'])}"
        f" precision={_format_rate(strict_nr['precision'])}"
        f" (tp={strict_nr['tp']} fn={strict_nr['fn']} fp={strict_nr['fp']} tn={strict_nr['tn']})"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 공고 추출 정확도 측정 CLI(골드 대조)"
    )
    parser.add_argument("--db", required=True, help="측정 대상 SQLite DB 경로")
    parser.add_argument(
        "--gold", required=True, help="골드 라벨 JSON 경로(data/notices/gold/gold_labels_v1.json)"
    )
    parser.add_argument("--out", required=True, help="결과 JSON 출력 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = db.get_connection(args.db)
    try:
        if count_all_extractions(conn) == 0:
            print(_EMPTY_EXTRACTIONS_HINT, file=sys.stderr)
            return 1

        gold_payload = json.loads(Path(args.gold).read_text(encoding="utf-8"))
        gold_version = gold_payload.get("version")
        gold_labels = gold_payload["labels"]

        extractions = load_extractions(conn, sorted(gold_labels))
        dataset_content_hash = queries.get_meta(conn).get("content_hash")
    finally:
        conn.close()

    report = build_report(gold_labels, extractions)

    payload = {
        "measured_at": _now_iso(),
        "db": args.db,
        "gold_version": gold_version,
        "dataset_content_hash": dataset_content_hash,
        **report,
    }
    _write_json(args.out, payload)
    print(_human_summary(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
