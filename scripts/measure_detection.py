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

## 예측 파일(``--predict-only`` 산출물) 스키마
metrics-spec의 공통 meta/results 헤더는 **최종 결과 JSON**(``--out``)에만 적용된다. 예측
파일은 "채점 이전의 중간 산출물"이라 별도의 단순 스키마를 쓴다(재구성에 필요한 최소
정보만 담음):
    {
      "dataset_content_hash": "...", "config_hash": "...", "params_ref": "...",
      "generated_at": "...", "sweep": {"start", "end", "days"},
      "predictions": {"<item_id>": {"<date ISO>": "<grade>", ...}, ...}
    }
``--score``는 이 파일의 dataset_content_hash·config_hash·params_ref·sweep을 그대로 최종
결과 JSON에 옮긴다 — 그래서 predict-only 시점에 쓰인 DB·params가 그대로 결과 meta에
반영되고, 일괄 실행과 2단계 실행이 같은 입력에서 항상 같은 결과를 낸다(generated_at만
채점 시각으로 달라진다).
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# 리포 루트를 sys.path에 올려 `medsupply`를 절대 경로 실행에서도 import할 수 있게 한다
# ("python scripts/measure_detection.py"로 직접 실행하면 sys.path[0]이 scripts/가 되어 리포
# 루트가 기본으로는 잡히지 않는다 — scripts/run_risk_batch.py와 동일한 처리).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from medsupply.analytics.params import AnalyticsParams, load_params  # noqa: E402
from medsupply.analytics.pipeline import assess_snapshot  # noqa: E402
from medsupply.data import db  # noqa: E402

#: '주의' 이상(주의·경고·위험) — 감지·오탐 판정에 쓰는 등급 집합.
ALERT_GRADES = frozenset({"주의", "경고", "위험"})
#: 최고등급 정밀도 분모를 이루는 등급.
DANGER_GRADE = "위험"

MEASURED_BY = "scripts/measure_detection.py"


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


def run_sweep(conn, start: date, end: date, params: AnalyticsParams) -> dict[str, dict[str, str]]:
    """[start, end] 매일 assess_snapshot을 실행해 {item_id: {date_iso: grade}}로 모은다.

    라벨을 전혀 읽지 않는다(인자로 받지도 않는다) — --predict-only가 이 함수만 호출하고
    끝낼 수 있는 이유다. 일자별 룩어헤드 차단은 assess_snapshot(as_of=d)이 이미 보장한다.
    """
    predictions: dict[str, dict[str, str]] = {}
    for d in _date_range(start, end):
        snapshot = assess_snapshot(conn, d, params)
        day_key = d.isoformat()
        for row in snapshot.itertuples(index=False):
            predictions.setdefault(row.item_id, {})[day_key] = row.grade
    return predictions


def _dataset_content_hash(conn) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = 'content_hash'").fetchone()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# 채점(순수 함수) — 스윕 결과 dict + 라벨 리스트 → 지표 dict. DB·파일 I/O 없음.
# ---------------------------------------------------------------------------


def _first_alert(day_grades: dict[str, str]) -> date | None:
    alert_days = sorted(
        date.fromisoformat(day) for day, grade in day_grades.items() if grade in ALERT_GRADES
    )
    return alert_days[0] if alert_days else None


def _ever_danger(day_grades: dict[str, str]) -> bool:
    return any(grade == DANGER_GRADE for grade in day_grades.values())


def _round_maybe(value: float | int) -> float | int:
    return round(value, 2) if isinstance(value, float) else value


def _lead_day_stats(lead_days: list[int]) -> dict[str, float | int | None]:
    if not lead_days:
        return {"min": None, "median": None, "mean": None, "max": None}
    return {
        "min": min(lead_days),
        "median": _round_maybe(statistics.median(lead_days)),
        "mean": _round_maybe(statistics.mean(lead_days)),
        "max": max(lead_days),
    }


def score_sweep(predictions: dict[str, dict[str, str]], labels: list[dict]) -> dict:
    """스윕 결과(predictions) + 라벨 리스트 → metrics-spec 지표 dict(순수 함수).

    Args:
        predictions: {item_id: {date_iso: grade}} — run_sweep의 반환 형식(또는 --score가
            읽은 예측 파일의 "predictions" 값). 이 함수의 "전체 품목" 모집단은 이 dict의
            키 전체다 — 라벨에 없는 품목은 정상으로 규정한다(docs/data-model.md §4).
        labels: ground truth 라벨 리스트(item_id·scenario_type·stockout_date 키 필요,
            나머지 필드는 무시).

    Returns:
        detection_rate, lead_days({min,median,mean,max}), false_positive_rate,
        danger_precision, by_type({scenario_type: {labeled,detected,detection_rate,
        lead_days}}), counts({labeled,normal,detected,false_positives})를 담은 dict.
        "sweep"(start/end/days)은 이 함수의 관심사가 아니다 — 호출부가 별도로 채운다.
        분모가 0인 비율(라벨 0건의 감지율, 정상 0건의 오탐률, 위험 판정 0건의 정밀도)은
        None으로 표기한다(0/0을 0.0으로 위장하지 않는다).
    """
    label_by_item = {row["item_id"]: row for row in labels}
    labeled_ids = set(label_by_item)
    normal_ids = set(predictions) - labeled_ids

    detected_ids: list[str] = []
    lead_days_all: list[int] = []
    labels_by_type: dict[str, list[str]] = {}
    success_lead_by_type: dict[str, list[int]] = {}

    for item_id, row in label_by_item.items():
        scenario_type = row["scenario_type"]
        stockout_date = date.fromisoformat(row["stockout_date"])
        labels_by_type.setdefault(scenario_type, []).append(item_id)

        day_grades = predictions.get(item_id, {})
        first_alert = _first_alert(day_grades)
        success = first_alert is not None and first_alert <= stockout_date
        if success:
            detected_ids.append(item_id)
            lead = (stockout_date - first_alert).days
            lead_days_all.append(lead)
            success_lead_by_type.setdefault(scenario_type, []).append(lead)

    false_positive_ids = [
        item_id for item_id in normal_ids if _first_alert(predictions[item_id]) is not None
    ]
    danger_ids = {item_id for item_id, grades in predictions.items() if _ever_danger(grades)}
    danger_hits = danger_ids & labeled_ids

    n_labeled = len(labeled_ids)
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

    return {
        "detection_rate": (len(detected_ids) / n_labeled) if n_labeled else None,
        "lead_days": _lead_day_stats(lead_days_all),
        "false_positive_rate": (len(false_positive_ids) / n_normal) if n_normal else None,
        "danger_precision": (len(danger_hits) / len(danger_ids)) if danger_ids else None,
        "by_type": by_type,
        "counts": {
            "labeled": n_labeled,
            "normal": n_normal,
            "detected": len(detected_ids),
            "false_positives": len(false_positive_ids),
        },
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
        predictions = run_sweep(conn, args.start, args.end, params)
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

    results = score_sweep(payload["predictions"], labels)
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
        predictions = run_sweep(conn, args.start, args.end, params)
        dataset_content_hash = _dataset_content_hash(conn)
    finally:
        conn.close()

    labels = _load_json(args.labels)
    results = score_sweep(predictions, labels)
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
