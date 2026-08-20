"""CLI 진입점 — 감지 성능 측정(스윕 백테스트, Task S-16).

**격리 원칙(중요)**: 이 스크립트(와 ``eval/``)만이 ``data/scenarios/ground_truth``의 라벨을
읽을 수 있는 유일하게 허용된 코드 경로다(docs/data-model.md §4). 라벨을 보고 판정하면 탐지
성능 측정 자체가 무의미해지므로, 스윕(``run_sweep``)은 라벨을 전혀 참조하지 않고
``medsupply.analytics.pipeline.assess_snapshot``만 호출한다 — 채점(``score_sweep``)만 라벨을
입력으로 받는다. ``--predict-only`` 경로는 그래서 ``--labels``를 아예 열지 않는다(존재하지
않는 경로를 줘도 성공해야 한다 — 블라인드 제출용).

사용법(일괄 실행 — 스윕→채점을 한 번에):
    python scripts/measure_detection.py --db data/medsupply.db \\
        --labels data/scenarios/ground_truth/standard_v1.json \\
        --start 2026-07-01 --end 2026-08-01 \\
        --out reports/analytics/detection_metrics.json [--params config/analytics_params.toml]

사용법(2단계 — 블라인드 제출/개봉):
    python scripts/measure_detection.py --db data/medsupply.db \\
        --start 2026-07-01 --end 2026-08-01 --predict-only predictions.json
    python scripts/measure_detection.py --score predictions.json \\
        --labels data/scenarios/ground_truth/standard_v1.json \\
        --out reports/analytics/detection_metrics.json

## 스윕·산식(docs/metrics-spec.md가 산식의 법전)
- 스윕: [start, end] 각 날짜 d에 대해 ``assess_snapshot(conn, d, params)``를 실행해 품목별
  (d, grade)를 기록한다. 룩어헤드 차단은 파이프라인(as_of=d)이 이미 보장한다.
- 감지: 품목의 스윕 중 '주의' 이상('주의'·'경고'·'위험') 최초 판정일이 first_alert. 라벨
  품목이 ``first_alert <= stockout_date``이면 감지 성공(stockout_date가 스윕 밖의 미래여도
  first_alert만 있으면 성공 — 조기 감지). first_alert가 없으면 미감지.
- 오탐: 비라벨(정상) 품목이 스윕 중 1회라도 '주의' 이상이면 오탐 품목 1건.
- 최고등급 정밀도: 스윕 중 1회라도 '위험' 판정된 품목 집합 중 라벨 품목의 비율(분모가 0이면
  판정 불가 → null).
- risk_type 일치율(Task S-17 확장): 라벨 품목별로 "스윕 중 최빈 risk_type"이 라벨
  scenario_type이 허용하는 risk_type 집합(``RISK_TYPE_MATCH_RULES``)에 드는지 본다.
  등급·감지·오탐 판정과는 **완전히 분리된 부가 지표**다(채택 기준의 3차 타이브레이크용).
- 이중 문턱 리포트(Task S-17b 확장): 위 감지·오탐 산식을 '경고 이상'(``threshold_warning``)·
  '위험 이상'(``threshold_danger``) 문턱으로 각각 한 번 더 계산해 **병기**한다. 문턱은
  ``threshold_metrics`` 한 함수의 인자일 뿐이라 감지와 오탐에 **대칭으로** 걸린다 — 오탐만
  유리하게 문턱을 올리는 비교가 구조적으로 불가능하다. 기본 문턱('주의 이상')의 기존 키·산식은
  그대로 두고 새 섹션만 추가한다.

## 예측 파일(``--predict-only`` 산출물) 스키마
metrics-spec의 공통 meta/results 헤더는 **최종 결과 JSON**(``--out``)에만 적용된다. 예측
파일은 "채점 이전의 중간 산출물"이라 별도의 단순 스키마를 쓴다(재구성에 필요한 최소
정보만 담음):
    {
      "dataset_content_hash": "...", "config_hash": "...", "params_ref": "...",
      "generated_at": "...", "sweep": {"start", "end", "days"},
      "predictions": {"<item_id>": {"<date ISO>": "<grade>", ...}, ...},
      "risk_types": {"<item_id>": {"<date ISO>": "<risk_type>", ...}, ...}
    }
``--score``는 이 파일의 dataset_content_hash·config_hash·params_ref·sweep을 그대로 최종
결과 JSON에 옮긴다 — 그래서 predict-only 시점에 쓰인 DB·params가 그대로 결과 meta에
반영되고, 일괄 실행과 2단계 실행이 같은 입력에서 항상 같은 결과를 낸다(generated_at만
채점 시각으로 달라진다). ``risk_types``는 S-17에서 추가된 필드로, 이것이 있어야 2단계
경로도 일괄 실행과 동일한 ``risk_type_match``를 산출한다 — 이 필드가 없는 옛 예측 파일을
``--score``에 넣으면 ``risk_type_match``만 null이 되고 나머지 지표는 그대로 나온다.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# ("python scripts/measure_detection.py"로 직접 실행하면 sys.path[0]이 scripts/가 되어 리포
# 루트가 기본으로는 잡히지 않는다 — scripts/run_risk_batch.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.analytics.params import AnalyticsParams, load_params  # noqa: E402
from medsupply.analytics.pipeline import assess_snapshot  # noqa: E402
from medsupply.data import db  # noqa: E402

#: '주의' 이상(주의·경고·위험) — 감지·오탐 판정에 쓰는 등급 집합(기본 문턱, 변경 금지).
ALERT_GRADES = frozenset({"주의", "경고", "위험"})
#: '경고' 이상 — 조치 등급 문턱(Task S-17b 이중 문턱 리포트).
WARNING_PLUS_GRADES = frozenset({"경고", "위험"})
#: '위험' 이상 — 최고 문턱 참고치(Task S-17b).
DANGER_PLUS_GRADES = frozenset({"위험"})
#: 최고등급 정밀도 분모를 이루는 등급.
DANGER_GRADE = "위험"

#: 등급 심각도 내림차순 — 문턱 집합을 결과 JSON에 적을 때의 표기 순서(가독성용).
GRADE_SEVERITY_ORDER = ("위험", "경고", "주의", "정상")

MEASURED_BY = "scripts/measure_detection.py"

#: 라벨 scenario_type → "일치"로 인정할 risk_type 집합(Task S-17 브리프 §3의 매핑 규칙).
#:
#: 브리프는 규칙을 ``usage_surge→demand_surge`` / ``supply_halt→supply_halt·composite`` /
#: ``delivery_delay→delivery_delay·composite`` / ``compound→composite``로 적는데, 실제 라벨
#: 파일이 쓰는 이름은 ``demand_surge``·``composite``다. 두 표기를 모두 키로 등록해 어느 쪽
#: 표기의 라벨이 들어와도 같은 판정이 나오게 한다(별칭이지 새 규칙이 아니다).
#:
#: composite를 허용하는 쪽이 supply_halt·delivery_delay뿐인 것은 의도다 — 브리프가 그 둘만
#: 허용으로 지정했다. demand_surge는 정확히 demand_surge여야 일치로 센다.
RISK_TYPE_MATCH_RULES: dict[str, frozenset[str]] = {
    "demand_surge": frozenset({"demand_surge"}),
    "usage_surge": frozenset({"demand_surge"}),
    "supply_halt": frozenset({"supply_halt", "composite"}),
    "delivery_delay": frozenset({"delivery_delay", "composite"}),
    "composite": frozenset({"composite"}),
    "compound": frozenset({"composite"}),
}


# ---------------------------------------------------------------------------
# 날짜 유틸
# ---------------------------------------------------------------------------


def _iso_date(value: str) -> date:
    """--start/--end 값을 ISO 날짜(YYYY-MM-DD)로 검증·변환한다."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ISO 날짜(YYYY-MM-DD)여야 한다: {value!r}") from exc


def _date_range(start: date, end: date) -> list[date]:
    """[start, end] 양끝 포함 날짜 목록(오름차순)."""
    n_days = (end - start).days
    return [start + timedelta(days=i) for i in range(n_days + 1)]


# ---------------------------------------------------------------------------
# 스윕(I/O) — 라벨을 절대 참조하지 않는다
# ---------------------------------------------------------------------------


def run_sweep_detail(
    conn, start: date, end: date, params: AnalyticsParams
) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """[start, end] 매일 assess_snapshot을 1회씩 실행해 등급 격자와 risk_type 격자를 함께 모은다.

    라벨을 전혀 읽지 않는다(인자로 받지도 않는다) — --predict-only가 이 함수만 호출하고
    끝낼 수 있는 이유다. 일자별 룩어헤드 차단은 assess_snapshot(as_of=d)이 이미 보장한다.

    등급과 risk_type을 한 번의 스윕에서 같이 뽑는 이유는 비용이다 — assess_snapshot은
    스윕에서 가장 비싼 호출이라 risk_type 때문에 스윕을 두 번 돌 수는 없다.

    Returns:
        (predictions, risk_types) — 둘 다 {item_id: {date_iso: 값}} 형식이고 키 집합이 같다.
    """
    predictions: dict[str, dict[str, str]] = {}
    risk_types: dict[str, dict[str, str]] = {}
    for d in _date_range(start, end):
        snapshot = assess_snapshot(conn, d, params)
        day_key = d.isoformat()
        for row in snapshot.itertuples(index=False):
            predictions.setdefault(row.item_id, {})[day_key] = row.grade
            risk_types.setdefault(row.item_id, {})[day_key] = row.risk_type
    return predictions, risk_types


def run_sweep(conn, start: date, end: date, params: AnalyticsParams) -> dict[str, dict[str, str]]:
    """[start, end] 매일 assess_snapshot을 실행해 {item_id: {date_iso: grade}}로 모은다.

    run_sweep_detail의 등급 격자만 뽑아 주는 얇은 래퍼다(기존 계약 유지 — 감지·오탐 판정에
    필요한 것은 등급뿐이다).
    """
    predictions, _ = run_sweep_detail(conn, start, end, params)
    return predictions


def _dataset_content_hash(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'content_hash'").fetchone()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# 채점(순수 함수) — 스윕 결과 dict + 라벨 리스트 → 지표 dict. DB·파일 I/O 없음.
# ---------------------------------------------------------------------------


def _first_alert(
    day_grades: dict[str, str], alert_grades: frozenset[str] = ALERT_GRADES
) -> date | None:
    """day_grades에서 alert_grades에 드는 최초 판정일. 없으면 None.

    alert_grades 기본값은 '주의' 이상(ALERT_GRADES)이라 기존 호출부의 동작은 그대로다.
    이중 문턱 리포트(S-17b)는 같은 함수에 '경고' 이상·'위험' 이상 집합을 넣어 **동일한 산식**을
    문턱만 바꿔 재사용한다.
    """
    alert_days = sorted(
        date.fromisoformat(day) for day, grade in day_grades.items() if grade in alert_grades
    )
    return alert_days[0] if alert_days else None


def _ever_danger(day_grades: dict[str, str]) -> bool:
    return any(grade == DANGER_GRADE for grade in day_grades.values())


def _round_maybe(value: float | int) -> float | int:
    return round(value, 2) if isinstance(value, float) else value


def modal_risk_type(day_risk_types: Mapping[str, str]) -> str | None:
    """스윕 기간 중 가장 자주 나온 risk_type. 관측이 없으면 None.

    동률은 risk_type 이름 사전순 오름차순으로 끊는다 — 임의의 dict 순서에 결과가 흔들리지
    않게 하기 위한 결정성 규칙이다(어느 쪽이 '더 옳은가'를 판단하지 않는다).
    """
    if not day_risk_types:
        return None
    counts = Counter(day_risk_types.values())
    top_count = max(counts.values())
    return sorted(name for name, count in counts.items() if count == top_count)[0]


def risk_type_matches(scenario_type: str, risk_type: str | None) -> bool:
    """risk_type이 scenario_type의 허용 집합(RISK_TYPE_MATCH_RULES)에 드는지.

    규칙에 없는 scenario_type은 "같은 이름이면 일치"로 대우한다(모르는 유형을 조용히 전부
    불일치로 깎지 않기 위한 보수적 기본값).
    """
    if risk_type is None:
        return False
    accepted = RISK_TYPE_MATCH_RULES.get(scenario_type, frozenset({scenario_type}))
    return risk_type in accepted


def _lead_day_stats(lead_days: list[int]) -> dict[str, float | int | None]:
    if not lead_days:
        return {"min": None, "median": None, "mean": None, "max": None}
    return {
        "min": min(lead_days),
        "median": _round_maybe(statistics.median(lead_days)),
        "mean": _round_maybe(statistics.mean(lead_days)),
        "max": max(lead_days),
    }


def threshold_metrics(
    predictions: dict[str, dict[str, str]],
    label_by_item: dict[str, dict],
    normal_ids: set[str],
    alert_grades: frozenset[str],
) -> tuple[dict, dict[str, dict], list[str]]:
    """한 문턱(alert_grades)에 대한 감지·오탐·선행일수·유형별 분해를 계산한다(순수 함수).

    **문턱을 제외한 산식은 문턱마다 완전히 동일하다** — 이 함수 하나를 '주의 이상'·'경고 이상'·
    '위험 이상'에 각각 적용하는 구조라, 문턱별로 유리한 산식을 따로 쓰는 일이 구조적으로
    불가능하다(S-17b: 문턱 변경은 감지·오탐 양쪽에 대칭 적용해야 지표 게이밍이 아니다).

    Args:
        predictions: {item_id: {date_iso: grade}}.
        label_by_item: {item_id: 라벨 dict} — scenario_type·stockout_date 키를 쓴다.
        normal_ids: 비라벨(정상) 품목 id 집합 — 오탐 모집단.
        alert_grades: 이 문턱에서 "알림"으로 칠 등급 집합.

    Returns:
        (report, per_item, false_positive_ids)
        - report: {alert_grades, detection_rate, lead_days, false_positive_rate, by_type, counts}
        - per_item: {item_id: {"first_alert": date|None, "detected": bool}} — 라벨 품목만.
        - false_positive_ids: 이 문턱에서 오탐으로 잡힌 정상 품목 id(정렬됨).
    """
    detected_ids: list[str] = []
    lead_days_all: list[int] = []
    labels_by_type: dict[str, list[str]] = {}
    success_lead_by_type: dict[str, list[int]] = {}
    per_item: dict[str, dict] = {}

    for item_id, row in label_by_item.items():
        scenario_type = row["scenario_type"]
        stockout_date = date.fromisoformat(row["stockout_date"])
        labels_by_type.setdefault(scenario_type, []).append(item_id)

        first_alert = _first_alert(predictions.get(item_id, {}), alert_grades)
        success = first_alert is not None and first_alert <= stockout_date
        if success:
            detected_ids.append(item_id)
            lead = (stockout_date - first_alert).days
            lead_days_all.append(lead)
            success_lead_by_type.setdefault(scenario_type, []).append(lead)

        per_item[item_id] = {"first_alert": first_alert, "detected": success}

    false_positive_ids = sorted(
        item_id
        for item_id in normal_ids
        if _first_alert(predictions[item_id], alert_grades) is not None
    )

    n_labeled = len(label_by_item)
    n_normal = len(normal_ids)

    by_type = {}
    for scenario_type, item_ids in sorted(labels_by_type.items()):
        n_type = len(item_ids)
        succ = success_lead_by_type.get(scenario_type, [])
        by_type[scenario_type] = {
            "labeled": n_type,
            "detected": len(succ),
            "detection_rate": (len(succ) / n_type) if n_type else None,
            "lead_days": _lead_day_stats(succ),
        }

    report = {
        "alert_grades": [g for g in GRADE_SEVERITY_ORDER if g in alert_grades],
        "detection_rate": (len(detected_ids) / n_labeled) if n_labeled else None,
        "lead_days": _lead_day_stats(lead_days_all),
        "false_positive_rate": (len(false_positive_ids) / n_normal) if n_normal else None,
        "by_type": by_type,
        "counts": {
            "labeled": n_labeled,
            "normal": n_normal,
            "detected": len(detected_ids),
            "false_positives": len(false_positive_ids),
        },
    }
    return report, per_item, false_positive_ids


def score_sweep(
    predictions: dict[str, dict[str, str]],
    labels: list[dict],
    risk_types: dict[str, dict[str, str]] | None = None,
) -> dict:
    """스윕 결과(predictions) + 라벨 리스트 → metrics-spec 지표 dict(순수 함수).

    Args:
        predictions: {item_id: {date_iso: grade}} — run_sweep의 반환 형식(또는 --score가
            읽은 예측 파일의 "predictions" 값). 이 함수의 "전체 품목" 모집단은 이 dict의
            키 전체다 — 라벨에 없는 품목은 정상으로 규정한다(docs/data-model.md §4).
        labels: ground truth 라벨 리스트(item_id·scenario_type·stockout_date 키 필요,
            나머지 필드는 무시).
        risk_types: {item_id: {date_iso: risk_type}} — run_sweep_detail의 두 번째 반환값
            (또는 예측 파일의 "risk_types" 값). None이면 risk_type 일치율을 계산할 근거가
            없으므로 결과의 "risk_type_match"가 통째로 None이 된다. 등급 기반 지표
            (감지율·오탐률·선행일수·정밀도)는 이 인자와 **무관하게** 항상 동일하다.

    Returns:
        detection_rate, lead_days({min,median,mean,max}), false_positive_rate,
        danger_precision, by_type({scenario_type: {labeled,detected,detection_rate,
        lead_days}}), counts({labeled,normal,detected,false_positives}),
        risk_type_match(아래 설명)를 담은 dict.
        "sweep"(start/end/days)은 이 함수의 관심사가 아니다 — 호출부가 별도로 채운다.
        분모가 0인 비율(라벨 0건의 감지율, 정상 0건의 오탐률, 위험 판정 0건의 정밀도)은
        None으로 표기한다(0/0을 0.0으로 위장하지 않는다).

        risk_type_match(Task S-17 신설 키)는
        {overall, counts{labeled,matched}, by_type{scenario_type:{labeled,matched,
        match_rate}}, items{item_id:{scenario_type, modal_risk_type, matched, detected,
        first_alert}}} 형태다. items는 라벨 품목별 감사 추적을 겸한다 — 채택 기준이
        요구하는 "유형별 개별 품목의 감지 유지 여부"를 라벨 접근이 허용된 이 경로에서만
        확인할 수 있게 하기 위해 detected·first_alert를 함께 싣는다.

        threshold_warning·threshold_danger(Task S-17b 신설 키)는 각각 '경고 이상'·'위험
        이상' 문턱으로 **같은 산식을**(threshold_metrics 한 함수) 다시 돌린 결과다:
        {alert_grades, detection_rate, lead_days, false_positive_rate, by_type, counts,
        false_positive_items}. 문턱이 감지와 오탐 **양쪽에 대칭으로** 걸리므로 한쪽만
        유리해지는 비교가 되지 않는다. 최상위 키('주의 이상' 기준)는 이 확장과 무관하게
        기존 값 그대로다.
    """
    label_by_item = {row["item_id"]: row for row in labels}
    labeled_ids = set(label_by_item)
    normal_ids = set(predictions) - labeled_ids

    watch_report, watch_items, _watch_fp_ids = threshold_metrics(
        predictions, label_by_item, normal_ids, ALERT_GRADES
    )
    warning_report, _, warning_fp_ids = threshold_metrics(
        predictions, label_by_item, normal_ids, WARNING_PLUS_GRADES
    )
    danger_report, _, danger_fp_ids = threshold_metrics(
        predictions, label_by_item, normal_ids, DANGER_PLUS_GRADES
    )
    warning_report["false_positive_items"] = warning_fp_ids
    danger_report["false_positive_items"] = danger_fp_ids

    match_items: dict[str, dict] = {}
    matched_by_type: dict[str, int] = {}
    labels_by_type: dict[str, list[str]] = {}

    for item_id, row in label_by_item.items():
        scenario_type = row["scenario_type"]
        labels_by_type.setdefault(scenario_type, []).append(item_id)

        if risk_types is not None:
            first_alert = watch_items[item_id]["first_alert"]
            item_modal = modal_risk_type(risk_types.get(item_id, {}))
            matched = risk_type_matches(scenario_type, item_modal)
            if matched:
                matched_by_type[scenario_type] = matched_by_type.get(scenario_type, 0) + 1
            match_items[item_id] = {
                "scenario_type": scenario_type,
                "modal_risk_type": item_modal,
                "matched": matched,
                "detected": watch_items[item_id]["detected"],
                "first_alert": first_alert.isoformat() if first_alert is not None else None,
            }

    danger_ids = {item_id for item_id, grades in predictions.items() if _ever_danger(grades)}
    danger_hits = danger_ids & labeled_ids

    n_labeled = len(labeled_ids)

    if risk_types is None:
        risk_type_match = None
    else:
        n_matched = sum(1 for entry in match_items.values() if entry["matched"])
        match_by_type = {}
        for scenario_type, item_ids in sorted(labels_by_type.items()):
            n_type = len(item_ids)
            n_type_matched = matched_by_type.get(scenario_type, 0)
            match_by_type[scenario_type] = {
                "labeled": n_type,
                "matched": n_type_matched,
                "match_rate": (n_type_matched / n_type) if n_type else None,
            }
        risk_type_match = {
            "overall": (n_matched / n_labeled) if n_labeled else None,
            "counts": {"labeled": n_labeled, "matched": n_matched},
            "by_type": match_by_type,
            "items": dict(sorted(match_items.items())),
        }

    return {
        "detection_rate": watch_report["detection_rate"],
        "lead_days": watch_report["lead_days"],
        "false_positive_rate": watch_report["false_positive_rate"],
        "danger_precision": (len(danger_hits) / len(danger_ids)) if danger_ids else None,
        "by_type": watch_report["by_type"],
        "counts": watch_report["counts"],
        "risk_type_match": risk_type_match,
        "threshold_warning": warning_report,
        "threshold_danger": danger_report,
    }


# ---------------------------------------------------------------------------
# 파일 I/O 헬퍼
# ---------------------------------------------------------------------------


def _load_json(path: str | Path) -> dict | list:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: str | Path, data: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _sweep_info(start: date, end: date) -> dict:
    return {"start": start.isoformat(), "end": end.isoformat(), "days": len(_date_range(start, end))}


# ---------------------------------------------------------------------------
# 사람이 읽는 stdout 요약
# ---------------------------------------------------------------------------


def _format_rate(value: float | None, numerator: int, denominator: int) -> str:
    if value is None:
        return f"n/a ({numerator}/{denominator})"
    return f"{value:.1%} ({numerator}/{denominator})"


def _human_summary(results: dict) -> str:
    counts = results["counts"]
    lines = [
        f"감지율: {_format_rate(results['detection_rate'], counts['detected'], counts['labeled'])}",
        f"오탐률: {_format_rate(results['false_positive_rate'], counts['false_positives'], counts['normal'])}",
    ]

    dp = results["danger_precision"]
    lines.append(f"최고등급 정밀도: {dp:.1%}" if dp is not None else "최고등급 정밀도: n/a(위험 판정 품목 없음)")

    ld = results["lead_days"]
    if ld["median"] is not None:
        lines.append(f"선행일수: min={ld['min']} median={ld['median']} mean={ld['mean']} max={ld['max']}")
    else:
        lines.append("선행일수: n/a(감지 성공 없음)")

    lines.append("유형별:")
    for scenario_type, bucket in results["by_type"].items():
        rate = bucket["detection_rate"]
        rate_str = f"{rate:.1%}" if rate is not None else "n/a"
        lines.append(f"  {scenario_type}: {bucket['detected']}/{bucket['labeled']} ({rate_str})")

    rtm = results.get("risk_type_match")
    if rtm is None:
        lines.append("risk_type 일치율: n/a(예측에 risk_type 없음)")
    else:
        lines.append(
            "risk_type 일치율: "
            + _format_rate(rtm["overall"], rtm["counts"]["matched"], rtm["counts"]["labeled"])
        )

    for key in ("threshold_warning", "threshold_danger"):
        section = results.get(key)
        if section is None:
            continue
        counts = section["counts"]
        median = section["lead_days"]["median"]
        lines.append(
            f"[{'·'.join(section['alert_grades'])} 이상 문턱] "
            f"감지 {_format_rate(section['detection_rate'], counts['detected'], counts['labeled'])} / "
            f"오탐 {_format_rate(section['false_positive_rate'], counts['false_positives'], counts['normal'])} / "
            f"선행 중앙값 {median if median is not None else 'n/a'}"
        )

    sw = results["sweep"]
    lines.append(f"스윕: {sw['start']} ~ {sw['end']} ({sw['days']}일)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 모드별 실행
# ---------------------------------------------------------------------------


def _finalize(results: dict, meta: dict, out_path: str) -> int:
    _write_json(out_path, {"meta": meta, "results": results})
    print(_human_summary(results))
    return 0


def _run_predict_only(args: argparse.Namespace) -> int:
    """스윕만 실행해 예측을 저장한다. --labels는 존재 여부조차 확인하지 않는다."""
    params = load_params(args.params)
    conn = db.get_connection(args.db)
    try:
        predictions, risk_types = run_sweep_detail(conn, args.start, args.end, params)
        dataset_content_hash = _dataset_content_hash(conn)
    finally:
        conn.close()

    payload = {
        "dataset_content_hash": dataset_content_hash,
        "config_hash": params.params_hash,
        "params_ref": args.params,
        "generated_at": _now_iso(),
        "sweep": _sweep_info(args.start, args.end),
        "predictions": predictions,
        "risk_types": risk_types,
    }
    _write_json(args.predict_only, payload)
    print(
        f"예측 저장 완료: {args.predict_only} "
        f"(품목 {len(predictions)}개 x {payload['sweep']['days']}일, 라벨 미참조)"
    )
    return 0


def _run_score(args: argparse.Namespace) -> int:
    """저장된 예측 파일 + --labels로 채점만 수행한다(블라인드 개봉)."""
    payload = _load_json(args.score)
    labels = _load_json(args.labels)

    results = score_sweep(payload["predictions"], labels, payload.get("risk_types"))
    results["sweep"] = payload["sweep"]

    meta = {
        "dataset_content_hash": payload["dataset_content_hash"],
        "config_hash": payload["config_hash"],
        "labels_version": Path(args.labels).name,
        "params_ref": payload["params_ref"],
        "generated_at": _now_iso(),
        "measured_by": MEASURED_BY,
    }
    return _finalize(results, meta, args.out)


def _run_full(args: argparse.Namespace) -> int:
    """스윕→채점을 한 번에 수행한다(기본 모드)."""
    params = load_params(args.params)
    conn = db.get_connection(args.db)
    try:
        predictions, risk_types = run_sweep_detail(conn, args.start, args.end, params)
        dataset_content_hash = _dataset_content_hash(conn)
    finally:
        conn.close()

    labels = _load_json(args.labels)
    results = score_sweep(predictions, labels, risk_types)
    results["sweep"] = _sweep_info(args.start, args.end)

    meta = {
        "dataset_content_hash": dataset_content_hash,
        "config_hash": params.params_hash,
        "labels_version": Path(args.labels).name,
        "params_ref": args.params,
        "generated_at": _now_iso(),
        "measured_by": MEASURED_BY,
    }
    return _finalize(results, meta, args.out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 감지 성능 측정 CLI(스윕 백테스트, 블라인드 2단계 지원)"
    )
    parser.add_argument("--db", help="스윕 대상 SQLite DB 경로")
    parser.add_argument("--labels", help="ground truth 라벨 JSON 경로(기본/--score 모드에서 필요)")
    parser.add_argument("--start", type=_iso_date, metavar="YYYY-MM-DD", help="스윕 시작일(포함)")
    parser.add_argument("--end", type=_iso_date, metavar="YYYY-MM-DD", help="스윕 종료일(포함)")
    parser.add_argument("--out", help="결과 JSON 출력 경로(기본/--score 모드에서 필요)")
    parser.add_argument(
        "--predict-only",
        dest="predict_only",
        metavar="PATH",
        help="라벨을 읽지 않고 스윕 예측(일자x품목 등급)만 PATH에 저장하고 종료(블라인드 제출용)",
    )
    parser.add_argument(
        "--score",
        dest="score",
        metavar="PATH",
        help="PATH의 예측 파일 + --labels로 채점만 수행(블라인드 개봉용)",
    )
    parser.add_argument(
        "--params",
        default="config/analytics_params.toml",
        help="분석 파라미터 TOML 경로(기본: config/analytics_params.toml)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    if args.predict_only and args.score:
        parser.error("--predict-only와 --score는 함께 지정할 수 없다")

    if args.score:
        if not args.labels or not args.out:
            parser.error("--score 모드에는 --labels·--out이 필요하다")
        return _run_score(args)

    if not args.db or not args.start or not args.end:
        parser.error("--db·--start·--end가 필요하다")

    if args.predict_only:
        return _run_predict_only(args)

    if not args.labels or not args.out:
        parser.error("기본 실행(스윕+채점)에는 --labels·--out이 필요하다")

    return _run_full(args)


if __name__ == "__main__":
    sys.exit(main())
