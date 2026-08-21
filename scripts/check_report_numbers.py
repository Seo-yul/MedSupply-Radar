"""검증 리포트의 수치를 측정 결과 JSON과 기계 대조한다(Task S-32).

`docs/verification-report.md`가 외부에 내보이는 증거물이 되려면 "문서가 주장하는 수치"와
"측정 스크립트가 실제로 산출한 값"이 사람 눈이 아니라 **기계로** 같아야 한다. 이 스크립트가
그 대조를 한다.

## 마킹 컨벤션

문서의 수치 옆(같은 줄 끝) 또는 **바로 다음 줄**에 HTML 주석으로 출처를 적는다.

    | 감지율 | 90.0% |
    <!-- check: reports/analytics/detection_metrics.json:results.detection_rate = 90.0% -->

- `<!-- check: {json 파일 경로}:{경로} = {문서 표기값} -->`
- 경로 문법은 **점 표기 + 배열 인덱스**뿐이다(`results.counts.detected`,
  `blind_results[0].lead_days.min`). jq 표현식·와일드카드·함수는 지원하지 않는다 — 단순한
  문법이라야 사람이 읽고 검증할 수 있다.
- 마킹 전용 줄(주석만 있는 줄)은 **바로 위 줄**에 붙는다. 표의 행이 길어지는 것을 막으려는
  장치이며, 위 줄이 비어 있으면 붙을 대상이 없으므로 문법 오류로 잡는다.
- `<!-- check-skip: {사유} -->`는 그 줄의 남은 숫자를 "측정값이 아님"으로 면제하되 **사유와
  면제된 토큰을 결과 JSON에 그대로 기록한다**(조용한 면제 금지).

표기값(`= ` 뒤)이 반올림 자리를 선언한다. `90.0%`는 "값 x 100을 소수 1자리로 half-up"이고
`45.19%`는 2자리다. `%p`도 백분율 환산으로 취급한다(`-2.56%p` ← -0.0256). `ms`·`일`·`건`
같은 단위는 표기값에 넣지 않는다 — JSON이 이미 그 단위로 저장하고 있기 때문이다.
`true`/`false`는 불리언, `"..."`는 문자열 완전 일치다.

## 세 가지 실패

1. **불일치** — 표기값이 JSON 실값과 다르다(또는 경로·파일이 없다, 표기값이 정작 그 줄에
   없다).
2. **미마킹** — 문서에 있는 숫자 중 어떤 마킹도 대조하지 않는 것이 남았다. 연도·ISO 날짜·
   식별자(ITM-0011·S-30b)·코드 스팬·§참조처럼 측정값일 수 없는 토큰은 규칙으로 면제하고,
   그 밖의 면제는 `check-skip` 주석으로만 가능하다.
3. **필수 마킹 누락** — 핵심 수치(감지·오탐·선행·MAPE·E2E·p95·재현성)의 경로가 문서 어디에도
   마킹돼 있지 않다. 대조는 통과하지만 정작 헤드라인이 빠진 상태를 막는다.

셋 다 0이면 exit 0, 하나라도 있으면 exit 1이다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: 대조 대상 문서(기본값).
DEFAULT_REPORT = "docs/verification-report.md"

#: 대조 결과 산출 경로(기본값).
DEFAULT_OUT = "reports/platform/report_check.json"

#: 마킹이 **반드시** 존재해야 하는 핵심 수치 경로. 브리프 §check_report_numbers:
#: "핵심 수치(감지·오탐·선행·MAPE·E2E·p95·재현성) 전부 마킹 의무".
REQUIRED_PATHS: dict[str, tuple[str, ...]] = {
    "reports/analytics/detection_metrics.json": (
        "results.detection_rate",
        "results.false_positive_rate",
        "results.lead_days.median",
        "results.danger_precision",
        "results.counts.detected",
        "results.counts.labeled",
        "results.counts.false_positives",
        "results.counts.normal",
        "results.threshold_warning.detection_rate",
        "results.threshold_warning.false_positive_rate",
        "results.within_horizon.threshold_watch.detection_rate",
        "results.risk_type_match.overall",
    ),
    "reports/analytics/forecast_mape.json": (
        "overall.ses_mape_mean",
        "overall.sma_mape_mean",
        "overall.baseline_improved",
        "overall.ses_win_rate",
    ),
    "reports/analytics/blind_summary.json": (
        "aggregate.detection_rate.mean",
        "aggregate.false_positive_rate.mean",
        "aggregate.lead_days_median.mean",
        "aggregate.risk_type_match_overall.mean",
    ),
    "reports/analytics/blind_round1_rescored.json": (
        "aggregate.within_horizon_current_criterion.detected",
        "aggregate.within_horizon_current_criterion.labeled_in_horizon",
        "aggregate.unscoreable_labels_total",
        "raw_metrics_identity_check.differing_paths_outside_horizon_view_total",
    ),
    "reports/analytics/blind_round2_summary.json": (
        "aggregate.detection_rate.mean",
        "aggregate.false_positive_rate.mean",
        "aggregate.lead_days_median.mean",
        "aggregate.risk_type_match_overall.mean",
        "aggregate.unscoreable_labels_total",
        "aggregate.attempts_used.mean",
    ),
    "reports/platform/e2e_results.json": (
        "runs",
        "passed_runs",
    ),
    "reports/platform/perf_results.json": (
        "targets.assess_snapshot.p95_ms",
        "targets.load_overview.p95_ms",
        "targets.list_items.p95_ms",
        "targets.load_item_detail.p95_ms",
        "targets.notice_detail_sweep.p95_ms",
    ),
    "reports/platform/reproducibility.json": (
        "runs",
        "generation.identical",
        "batch.identical",
        "detection.identical",
        "generation.anchor_match",
    ),
}


class PathError(Exception):
    """JSON 경로를 해석할 수 없다(키 부재·인덱스 초과·타입 불일치)."""


class MarkSyntaxError(Exception):
    """마킹 주석 문법 오류 — 조용히 무시하지 않고 즉시 실패시킨다."""


# ---------------------------------------------------------------------------
# JSON 경로 해석 — 점 표기 + 배열 인덱스만
# ---------------------------------------------------------------------------

_SEGMENT_RE = re.compile(r"^(?P<name>[A-Za-z0-9_][A-Za-z0-9_-]*)(?P<idx>(?:\[\d+\])*)$")
_INDEX_RE = re.compile(r"\[(\d+)\]")


def resolve_path(data: object, path: str):
    """`a.b[0].c` 형태의 경로로 JSON 값을 꺼낸다. 실패는 전부 PathError."""
    current = data
    walked: list[str] = []
    for segment in path.split("."):
        matched = _SEGMENT_RE.match(segment)
        if not matched:
            raise PathError(f"경로 문법 오류: {segment!r} (경로 {path!r})")
        name = matched.group("name")
        walked.append(name)
        if not isinstance(current, dict) or name not in current:
            raise PathError(f"경로 없음: {'.'.join(walked)}")
        current = current[name]
        for index_text in _INDEX_RE.findall(matched.group("idx")):
            index = int(index_text)
            walked[-1] = f"{walked[-1]}[{index}]"
            if not isinstance(current, list):
                raise PathError(f"배열이 아님: {'.'.join(walked)}")
            if index >= len(current):
                raise PathError(f"인덱스 초과: {'.'.join(walked)} (길이 {len(current)})")
            current = current[index]
    return current


# ---------------------------------------------------------------------------
# 표기값 ↔ 실값 비교
# ---------------------------------------------------------------------------

_NUMERIC_LITERAL_RE = re.compile(r"^(?P<num>-?\d+(?:\.\d+)?)(?P<unit>%p|%)?$")


def _json_repr(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _format_number(value: float, decimals: int, scale: int) -> str:
    """value * scale을 소수 decimals자리로 half-up 반올림한 문자열.

    float 이진 오차가 표기 판정을 흔들지 않도록 Decimal(str(value))로 들어간다
    (0.4519230769230769 → '45.19'가 부동소수 잔차 없이 나온다).
    """
    scaled = Decimal(str(value)) * scale
    quantum = Decimal(1).scaleb(-decimals)
    text = f"{scaled.quantize(quantum, rounding=ROUND_HALF_UP):f}"
    if text.startswith("-") and set(text[1:]) <= {"0", "."}:
        text = text[1:]  # -0.0 → 0.0
    return text


def compare_literal(value: object, literal: str) -> tuple[bool, str]:
    """(일치 여부, 기대 표기값). 기대 표기값은 사람이 읽을 실패 메시지용이다."""
    if literal in ("true", "false"):
        want = literal == "true"
        return (value is want), _json_repr(value)
    if len(literal) >= 2 and literal.startswith('"') and literal.endswith('"'):
        return (value == literal[1:-1]), _json_repr(value)

    matched = _NUMERIC_LITERAL_RE.match(literal)
    if not matched:
        raise MarkSyntaxError(f"표기값 문법 오류: {literal!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, _json_repr(value)

    num_text = matched.group("num")
    decimals = len(num_text.split(".")[1]) if "." in num_text else 0
    scale = 100 if matched.group("unit") else 1
    formatted = _format_number(value, decimals, scale)
    return (formatted == num_text), formatted


# ---------------------------------------------------------------------------
# 마킹 파싱
# ---------------------------------------------------------------------------

_MARK_RE = re.compile(r"<!--\s*check:\s*(?P<body>.+?)\s*-->")
_SKIP_RE = re.compile(r"<!--\s*check-skip:\s*(?P<reason>.+?)\s*-->")
_ANY_COMMENT_RE = re.compile(r"<!--.*?-->")
_BODY_RE = re.compile(
    r"^(?P<source>[A-Za-z0-9_./-]+\.json)\s*:\s*(?P<path>[A-Za-z0-9_.\[\]-]+)\s*=\s*(?P<literal>.+)$"
)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Mark:
    """문서 표기 하나와 그 출처 JSON 경로의 결속."""

    source: str
    path: str
    literal: str
    target_line_no: int  # 1-base
    mark_line_no: int


@dataclass(frozen=True)
class Skip:
    """그 줄의 남은 숫자를 면제하는 지시(사유 필수)."""

    reason: str
    target_line_no: int
    mark_line_no: int


_CODE_SPAN_RE = re.compile(r"`[^`]*`")


def _strip_code_spans(line: str) -> str:
    """코드 스팬을 같은 길이의 공백으로 지운다.

    백틱 안의 `<!-- check: ... -->`는 **지시가 아니라 예시 텍스트**다(이 파일 상단의 컨벤션
    설명이나 리포트의 사용법 안내가 그렇다). 지시로 읽으면 문서가 자기 설명을 못 쓴다.
    """
    return _CODE_SPAN_RE.sub(lambda matched: " " * len(matched.group(0)), line)


def _is_mark_only_line(line: str) -> bool:
    """주석을 걷어내면 아무것도 남지 않는 줄(= 마킹 전용 줄)인지."""
    if "<!--" not in line:
        return False
    return _ANY_COMMENT_RE.sub("", line).strip() == ""


def parse_marks(lines: list[str]) -> tuple[list[Mark], list[Skip]]:
    """문서 줄 목록에서 마킹·면제 지시를 뽑는다(코드 펜스 안은 무시)."""
    marks: list[Mark] = []
    skips: list[Skip] = []
    in_fence = False

    for index, raw_line in enumerate(lines):
        line_no = index + 1
        if _FENCE_RE.match(raw_line):
            in_fence = not in_fence
            continue
        line = _strip_code_spans(raw_line)
        if in_fence or "<!--" not in line:
            continue

        if _is_mark_only_line(line):
            if index == 0 or not lines[index - 1].strip() or _is_mark_only_line(lines[index - 1]):
                raise MarkSyntaxError(
                    f"{line_no}행: 마킹 전용 줄이 붙을 본문 줄이 바로 위에 없다"
                )
            target_line_no = index  # 바로 위 줄(1-base로는 index)
        else:
            target_line_no = line_no

        for matched in _MARK_RE.finditer(line):
            body = matched.group("body")
            parsed = _BODY_RE.match(body)
            if not parsed:
                raise MarkSyntaxError(f"{line_no}행: 마킹 문법 오류 — {body!r}")
            marks.append(
                Mark(
                    source=parsed.group("source"),
                    path=parsed.group("path"),
                    literal=parsed.group("literal").strip(),
                    target_line_no=target_line_no,
                    mark_line_no=line_no,
                )
            )
        for matched in _SKIP_RE.finditer(line):
            skips.append(
                Skip(
                    reason=matched.group("reason").strip(),
                    target_line_no=target_line_no,
                    mark_line_no=line_no,
                )
            )
    return marks, skips


# ---------------------------------------------------------------------------
# 숫자 토큰화와 면제 규칙
# ---------------------------------------------------------------------------

#: 숫자 토큰 = (선택)부호 + 숫자 + (선택)소수부 + (선택)% / %p.
#: 앞에 영문자·숫자·점이 붙어 있으면 토큰으로 세지 않는다 — `p95`의 95, `v1`의 1처럼
#: 식별자의 일부인 숫자를 측정값으로 오인하지 않기 위해서다.
_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_.])-?\d+(?:\.\d+)?(?:%p|%)?")

#: 면제 규칙(순서 있음 — 앞의 규칙이 먼저 먹는다). 측정값일 수 없는 토큰만 담는다.
ALLOW_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("html_comment", _ANY_COMMENT_RE),
    ("code_span", re.compile(r"`[^`]*`")),
    ("table_separator", re.compile(r"^\|[\s:\-|]+\|\s*$")),
    ("heading_number", re.compile(r"^#{1,6}\s+\d+(?:\.\d+)*\.?")),
    ("list_number", re.compile(r"^\s*\d+\.(?=\s)")),
    ("iso_date", re.compile(r"\d{4}-\d{2}-\d{2}")),
    ("seed", re.compile(r"(?<!\d)20\d{6}(?!\d)")),
    ("identifier", re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}-\d{1,4}[a-z]?(?![0-9])")),
    ("audit_ref", re.compile(r"(?<![A-Za-z0-9])[WHF]\d{1,2}[a-z]?(?![0-9])")),
    ("section_ref", re.compile(r"§\s*\d+(?:\.\d+)*")),
    ("ko_ordinal", re.compile(r"(?<!\d)\d+(?:주차|차|단계|번)(?![0-9])")),
    ("version", re.compile(r"(?<![A-Za-z0-9])v\d+(?:\.\d+)*(?![0-9])")),
    ("percentile_label", re.compile(r"(?<![A-Za-z0-9])p\d{2}(?![0-9])")),
    ("year", re.compile(r"(?<!\d)20\d{2}(?!\d)")),
)


def number_tokens(text: str) -> list[str]:
    """텍스트에서 대조 대상이 될 수 있는 숫자 표기를 순서대로 뽑는다."""
    return _TOKEN_RE.findall(text)


def visible_text(line: str) -> tuple[str, list[dict]]:
    """면제 규칙을 적용한 뒤 남은 텍스트와, 각 규칙이 걷어낸 내역을 돌려준다.

    걷어낸 자리는 공백으로 바꾼다(같은 규칙이 인접 토큰을 잇지 않게 하고, 열 위치도 대략
    보존한다). 내역에는 그 규칙이 삼킨 숫자 토큰을 함께 적어 "무엇이 면제됐는지"가
    결과 JSON에 남게 한다.
    """
    text = line
    allowed: list[dict] = []
    for rule, pattern in ALLOW_RULES:
        while True:
            matched = pattern.search(text)
            if not matched:
                break
            chunk = matched.group(0)
            allowed.append(
                {"rule": rule, "text": chunk.strip(), "tokens": number_tokens(chunk)}
            )
            text = text[: matched.start()] + " " * len(chunk) + text[matched.end() :]
    return text, allowed


# ---------------------------------------------------------------------------
# 종단 대조
# ---------------------------------------------------------------------------


def _load_sources(marks: list[Mark], base_dir: Path) -> tuple[dict, dict]:
    """마킹이 참조하는 JSON들을 한 번씩만 읽고 sha256을 남긴다(감사 결속)."""
    data: dict[str, object] = {}
    meta: dict[str, dict] = {}
    for source in sorted({mark.source for mark in marks}):
        path = base_dir / source
        if not path.exists():
            meta[source] = {"exists": False, "error": "파일 없음"}
            continue
        raw = path.read_bytes()
        try:
            data[source] = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            meta[source] = {"exists": True, "error": f"JSON 파싱 실패: {exc}"}
            continue
        meta[source] = {"exists": True, "sha256": hashlib.sha256(raw).hexdigest()}
    return data, meta


def check_report(
    report_path: Path | str,
    base_dir: Path | str,
    required: dict[str, tuple[str, ...]] | None = None,
) -> dict:
    """리포트 md를 대조하고 결과 dict를 돌려준다(파일 쓰기는 호출부 책임)."""
    report_path = Path(report_path)
    base_dir = Path(base_dir)
    lines = report_path.read_text(encoding="utf-8").splitlines()

    marks, skips = parse_marks(lines)
    sources, source_meta = _load_sources(marks, base_dir)

    marks_by_line: dict[int, list[Mark]] = {}
    for mark in marks:
        marks_by_line.setdefault(mark.target_line_no, []).append(mark)
    skips_by_line: dict[int, list[Skip]] = {}
    for skip in skips:
        skips_by_line.setdefault(skip.target_line_no, []).append(skip)

    checked: list[dict] = []
    mismatches: list[dict] = []
    unmarked: list[dict] = []
    skip_rows: list[dict] = []
    allowed_counter: Counter[str] = Counter()

    in_fence = False
    for index, line in enumerate(lines):
        line_no = index + 1
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        text, allowed = visible_text(line)
        for row in allowed:
            allowed_counter[row["rule"]] += len(row["tokens"])
        remaining = Counter(number_tokens(text))

        for mark in marks_by_line.get(line_no, []):
            record = {
                "line": line_no,
                "source": mark.source,
                "path": mark.path,
                "literal": mark.literal,
            }
            meta = source_meta.get(mark.source, {})
            if mark.source not in sources:
                mismatches.append(
                    {**record, "kind": "source_error", "detail": meta.get("error", "읽기 실패")}
                )
                continue
            try:
                value = resolve_path(sources[mark.source], mark.path)
            except PathError as exc:
                mismatches.append({**record, "kind": "path_error", "detail": str(exc)})
                continue

            ok, formatted = compare_literal(value, mark.literal)
            # 불리언·문자열 표기는 본문에 그 글자가 그대로 찍히지 않는다("5회 동일"은
            # identical=true의 우리말 서술이다). 숫자 표기만 "그 줄에 실제로 있는가"를 따진다.
            numeric = bool(_NUMERIC_LITERAL_RE.match(mark.literal))
            present = (not numeric) or remaining.get(mark.literal, 0) > 0
            if numeric and present:
                remaining[mark.literal] -= 1

            if not ok:
                mismatches.append(
                    {
                        **record,
                        "kind": "value_mismatch",
                        "expected": formatted,
                        "json_value": value,
                    }
                )
            elif not present:
                mismatches.append(
                    {
                        **record,
                        "kind": "literal_not_in_line",
                        "detail": "표기값이 그 줄의 본문에 없다",
                        "line_text": text.strip(),
                    }
                )
            else:
                checked.append(
                    {**record, "json_value": value, "literal_found_in_line": numeric}
                )

        leftover = sorted(token for token, count in remaining.items() for _ in range(count))
        if leftover and line_no in skips_by_line:
            for skip in skips_by_line[line_no]:
                skip_rows.append(
                    {"line": line_no, "reason": skip.reason, "tokens": leftover}
                )
                leftover = []
                break
        elif line_no in skips_by_line:
            for skip in skips_by_line[line_no]:
                skip_rows.append({"line": line_no, "reason": skip.reason, "tokens": []})
        for token in leftover:
            unmarked.append({"line": line_no, "token": token, "line_text": line.strip()})

    marked_pairs = {f"{mark.source}:{mark.path}" for mark in marks}
    required = required or {}
    required_all = [
        f"{source}:{path}" for source, paths in required.items() for path in paths
    ]
    missing_required = [key for key in required_all if key not in marked_pairs]

    status = "PASS" if not mismatches and not unmarked and not missing_required else "FAIL"
    return {
        "report": str(report_path.relative_to(base_dir))
        if report_path.is_relative_to(base_dir)
        else str(report_path),
        "generated_at": datetime.now().replace(microsecond=0).isoformat(),
        "checked_by": "scripts/check_report_numbers.py",
        "summary": {
            "status": status,
            "marks": len(marks),
            "matched": len(checked),
            "mismatches": len(mismatches),
            "unmarked": len(unmarked),
            "skipped_lines": len(skip_rows),
            "skipped_tokens": sum(len(row["tokens"]) for row in skip_rows),
            "required_paths": len(required_all),
            "missing_required_paths": len(missing_required),
        },
        "sources": source_meta,
        "marks": checked,
        "mismatches": mismatches,
        "unmarked": unmarked,
        "skips": skip_rows,
        "allowed_tokens_by_rule": dict(sorted(allowed_counter.items())),
        "required_paths": {"declared": required_all, "missing": missing_required},
    }


def _human_summary(result: dict) -> str:
    summary = result["summary"]
    lines = [
        f"[{summary['status']}] {result['report']} — 마킹 {summary['marks']}건"
        f"(일치 {summary['matched']}) / 불일치 {summary['mismatches']}"
        f" / 미마킹 {summary['unmarked']}"
        f" / 면제 {summary['skipped_tokens']}토큰({summary['skipped_lines']}줄)"
        f" / 필수 마킹 누락 {summary['missing_required_paths']}",
    ]
    for row in result["mismatches"]:
        detail = row.get("expected") or row.get("detail") or ""
        lines.append(
            f"  불일치[{row['kind']}] {row['line']}행 {row['source']}:{row['path']}"
            f" — 표기 {row['literal']!r} vs {detail!r}"
        )
    for row in result["unmarked"]:
        lines.append(f"  미마킹 {row['line']}행 — {row['token']} ({row['line_text'][:60]})")
    for key in result["required_paths"]["missing"]:
        lines.append(f"  필수 마킹 누락 — {key}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="검증 리포트의 수치를 reports/의 측정 JSON과 기계 대조한다"
    )
    parser.add_argument("--report", default=str(REPO_ROOT / DEFAULT_REPORT), help="대조할 md 경로")
    parser.add_argument("--base-dir", default=str(REPO_ROOT), help="JSON 경로의 기준 디렉터리")
    parser.add_argument("--out", default=str(REPO_ROOT / DEFAULT_OUT), help="대조 결과 JSON 경로")
    parser.add_argument(
        "--no-required",
        action="store_true",
        help="필수 마킹 검사를 끈다(픽스처 대조용 — 실제 리포트 대조에서는 쓰지 않는다)",
    )
    args = parser.parse_args(argv)

    result = check_report(
        args.report,
        args.base_dir,
        required=None if args.no_required else REQUIRED_PATHS,
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(_human_summary(result))
    return 0 if result["summary"]["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
