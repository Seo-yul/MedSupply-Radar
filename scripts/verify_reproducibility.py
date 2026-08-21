"""CLI 진입점 — subprocess 수준 재현성 5회 검증(Task S-23).

"동일 명령 → 동일 산출"이라는 기획서 공개 약속을 코드 자체가 아니라 **subprocess**
수준에서(각 대상 스크립트를 별도 프로세스로 N회 실행) 실측·기록한다. 대상 3계열:

1. **생성 재현**: `scripts/generate_dataset.py`(표준 config) + `scripts/load_notices.py`를
   tmp 출력에 N회 실행 — 각 산출 DB의 `meta.content_hash`가 N회 전부 동일한지, 그리고
   그 값이 `data/scenarios/standard_snapshot.sha256`의 봉인 앵커와 일치하는지 확인한다.
2. **배치 재현**: 1의 산출 DB 1개를 N개 사본으로 복제해 각각 `scripts/run_risk_batch.py`를
   실행 — `risk_results`의 (item_id, grade, score, days_to_stockout) 튜플 집합과 run_id가
   N회 전부 동일한지 확인한다.
3. **측정 재현**: 표준 스냅샷(`data/medsupply.db`, 원본·읽기 전용)에 `scripts/
   measure_detection.py`를 동일 인자로 N회 실행 — 결과 JSON의 "results" 딕셔너리가
   N회 전부 동일한지 확인한다(타임스탬프 계열 키는 비교에서 제외).

**격리 원칙**: 이 스크립트는 위 4개 스크립트를 전부 subprocess로만 호출한다(import하지
않는다 — `medsupply`도, `scripts.datagen`도, `scripts.measure_detection`도 import하지
않는다). 정답 라벨(ground truth) 경로는 이 파일 어디에도 코드 값으로 등장하지 않는다 —
`--labels` CLI 인자로 호출부가 넘긴 값을 그대로 `measure_detection.py` subprocess에
전달만 할 뿐, 이 스크립트 자신은 그 파일을 열거나 파싱하지 않는다(순수 위임 호출 —
tests/test_isolation.py의 SCRIPTS_PATH_TARGETS 등록 사유 그대로). `data/scenarios`는
표준 시나리오 config·봉인 앵커 파일 경로 참조(설정 로딩) 목적으로만 등장한다(예외
등록됨 — generate_dataset.py·validate_dataset.py와 동일 사유).

사용법:
    python scripts/verify_reproducibility.py --runs 5 \\
        --out reports/platform/reproducibility.json \\
        --labels data/scenarios/ground_truth/standard_v1.json \\
        --detection-start 2026-07-01 --detection-end 2026-08-01

생성 재현 5회는 매번 전체 스냅샷을 새로 빌드하므로 시간이 걸린다 — 각 하위 단계마다
진행 로그를 stdout에 찍는다. tmp 산출물은 전부 `tempfile.TemporaryDirectory` 안에서만
만들어지고, 검증이 끝나면(성공·실패 무관) 자동 정리된다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_dataset.py"
LOAD_NOTICES_SCRIPT = REPO_ROOT / "scripts" / "load_notices.py"
RUN_RISK_BATCH_SCRIPT = REPO_ROOT / "scripts" / "run_risk_batch.py"
MEASURE_DETECTION_SCRIPT = REPO_ROOT / "scripts" / "measure_detection.py"

#: 표준 빌드 1단계 config(data/scenarios/standard_snapshot.sha256 주석의 명령 그대로).
SCENARIO_CONFIG_PATH = REPO_ROOT / "data" / "scenarios" / "scenario_config.yaml"
#: 봉인 앵커(부트스트랩 content_hash) — 생성 재현이 이 값과 일치하는지 확인하는 기준.
STANDARD_ANCHOR_PATH = REPO_ROOT / "data" / "scenarios" / "standard_snapshot.sha256"
#: 표준 빌드 2단계(공고 적재) 입력.
NOTICES_RAW_DIR = REPO_ROOT / "data" / "notices" / "raw"
NOTICES_INDEX_PATH = REPO_ROOT / "data" / "notices" / "notices_index.csv"
#: 측정 재현 대상 — 표준 스냅샷 원본(읽기 전용, 절대 수정하지 않는다).
STANDARD_DB_PATH = REPO_ROOT / "data" / "medsupply.db"

#: 생성 재현 고정 파라미터(표준 스냅샷 빌드 명령과 동일 — 브리프 지정값, 변경 금지).
GENERATION_SEED = 20260801
GENERATION_BASE_DATE = "2026-08-01"
#: 배치 재현 고정 as-of(브리프 지정값).
BATCH_AS_OF = "2026-08-01"
#: 두 배치·측정 스크립트가 공통으로 쓰는 분석 파라미터 기본 경로(각 스크립트 자체 기본값과 동일).
DEFAULT_PARAMS_PATH = "config/analytics_params.toml"

#: 결과 JSON에서 계열 간 실행 시각 차이만으로 mismatch 처리되지 않게 제외할 키.
_TIMESTAMP_KEYS: tuple[str, ...] = ("measured_at", "generated_at")


# ---------------------------------------------------------------------------
# 순수 비교/다이제스트 헬퍼 — 소형 픽스처로 단위 검증 가능(무거운 I/O 없음)
# ---------------------------------------------------------------------------


def compare_values(values: list[str]) -> tuple[bool, dict | None]:
    """스칼라 값 리스트(content_hash·run_id 등)가 전부 동일한지 비교한다.

    Returns:
        (identical, mismatch) — identical이면 mismatch는 None. 아니면
        {"reference": 첫 값, "mismatches": [{"run": idx, "value": 실제값}, ...]}.
    """
    if not values:
        return True, None
    reference = values[0]
    mismatches = [
        {"run": i, "value": v} for i, v in enumerate(values) if v != reference
    ]
    if mismatches:
        return False, {"reference": reference, "mismatches": mismatches}
    return True, None


def compare_row_sets(row_sets: list[list[tuple]]) -> tuple[bool, dict | None]:
    """런별 (item_id, grade, score, days_to_stockout) 튜플 리스트가 집합으로 전부 동일한지.

    행 순서는 무관(집합 비교) — SELECT 정렬 여부와 무관하게 내용만 비교한다.

    Returns:
        (identical, mismatch) — mismatch는 {"reference_row_count", "mismatches":
        [{"run", "missing_from_run", "extra_in_run"}, ...]}(0번 런 기준 대칭차, 최대 20건 샘플).
    """
    if not row_sets:
        return True, None
    sets = [frozenset(rs) for rs in row_sets]
    reference = sets[0]
    mismatches = []
    for i, s in enumerate(sets[1:], start=1):
        only_in_ref = sorted(reference - s)
        only_in_run = sorted(s - reference)
        if only_in_ref or only_in_run:
            mismatches.append(
                {
                    "run": i,
                    "missing_from_run": [list(row) for row in only_in_ref[:20]],
                    "extra_in_run": [list(row) for row in only_in_run[:20]],
                }
            )
    if mismatches:
        return False, {"reference_row_count": len(reference), "mismatches": mismatches}
    return True, None


def compare_json_dicts(
    dicts: list[dict], ignore_keys: tuple[str, ...] = ()
) -> tuple[bool, dict | None]:
    """결과 딕셔너리(예: measure_detection.py의 "results")가 ignore_keys를 뺀 나머지
    전부 동일한지 비교한다.

    Returns:
        (identical, mismatch) — mismatch는 {"mismatches": [{"run", "differing_keys"}, ...]}.
    """
    stripped = [{k: v for k, v in d.items() if k not in ignore_keys} for d in dicts]
    if not stripped:
        return True, None
    reference = stripped[0]
    mismatches = []
    _missing = object()
    for i, d in enumerate(stripped[1:], start=1):
        if d != reference:
            all_keys = sorted(set(reference) | set(d))
            differing = [
                k for k in all_keys if reference.get(k, _missing) != d.get(k, _missing)
            ]
            mismatches.append({"run": i, "differing_keys": differing})
    if mismatches:
        return False, {"mismatches": mismatches}
    return True, None


def compute_verdict(generation: dict, batch: dict, detection: dict) -> bool:
    """3계열 전부 identical일 때만 True(브리프: "verdict = 3계열 전부 identical")."""
    return bool(generation["identical"] and batch["identical"] and detection["identical"])


def sha256_of_json(obj: object) -> str:
    """obj를 키 정렬 JSON으로 직렬화해 sha256 hex 다이제스트를 만든다(순서 무관 결정적)."""
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def read_anchor_hash(path: str | Path = STANDARD_ANCHOR_PATH) -> str:
    """봉인 앵커 파일(주석 N줄 + 해시 1줄 포맷)에서 첫 비-주석 줄을 읽는다.

    scripts/validate_dataset.py의 resolve_expected_hash와 같은 포맷을 읽지만, 이 파일은
    다른 스크립트를 import하지 않는다는 원칙(전부 subprocess로만 위임)을 지키기 위해
    파서를 독립적으로 다시 구현했다(3~4줄짜리 얇은 파싱이라 복제 비용이 낮다).
    """
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    raise ValueError(f"{path}에서 해시 값을 찾지 못했다(주석이 아닌 줄이 없음)")


# ---------------------------------------------------------------------------
# 소형 DB 조회 헬퍼(읽기 전용) — sqlite3 직접 사용, medsupply 미import
# ---------------------------------------------------------------------------


def _sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_meta_value(db_path: str | Path, key: str) -> str | None:
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def _read_risk_results(db_path: str | Path) -> tuple[str, list[tuple]]:
    """risk_results에서 (run_id, 정렬된 (item_id, grade, score, days_to_stockout) 리스트)를
    읽는다. run_id가 정확히 1종이 아니면(배치를 정확히 1회만 실행했다는 전제 위반) 명확한
    에러로 실패한다."""
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        run_ids = [
            row[0] for row in conn.execute("SELECT DISTINCT run_id FROM risk_results")
        ]
        rows = conn.execute(
            "SELECT item_id, grade, score, days_to_stockout FROM risk_results"
            " ORDER BY item_id"
        ).fetchall()
    finally:
        conn.close()
    if len(run_ids) != 1:
        raise RuntimeError(
            f"{db_path}: risk_results에 run_id가 {len(run_ids)}종 존재(정확히 1종 기대): {run_ids}"
        )
    return run_ids[0], [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# subprocess 실행 헬퍼
# ---------------------------------------------------------------------------


def _run_script(script: Path, args: list[str], *, description: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [sys.executable, str(script), *args], capture_output=True, text=True, cwd=REPO_ROOT
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"{description} 실패(exit {proc.returncode})\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


# ---------------------------------------------------------------------------
# 계열 1 — 생성 재현
# ---------------------------------------------------------------------------


def run_generation_series(
    runs: int,
    work_dir: Path,
    *,
    seed: int = GENERATION_SEED,
    base_date: str = GENERATION_BASE_DATE,
    log: Callable[[str], None] = print,
) -> tuple[dict, list[Path]]:
    """generate_dataset.py + load_notices.py를 표준 2단계 빌드 명령 그대로 N회 실행한다.

    Returns:
        (series_result, db_paths) — db_paths는 배치 재현(계열 2)이 그중 1개를 복제해
        쓸 수 있도록 반환한다.
    """
    hashes: list[str] = []
    db_paths: list[Path] = []
    for i in range(runs):
        t0 = time.monotonic()
        log(f"[생성 재현 {i + 1}/{runs}] generate_dataset.py 실행 중...")
        run_dir = work_dir / f"gen_{i}"
        run_dir.mkdir(parents=True, exist_ok=True)
        out_db = run_dir / "medsupply.db"

        _run_script(
            GENERATE_SCRIPT,
            [
                "--config", str(SCENARIO_CONFIG_PATH),
                "--out", str(out_db),
                "--seed", str(seed),
                "--base-date", base_date,
            ],
            description=f"generate_dataset.py(run {i + 1}/{runs})",
        )
        _run_script(
            LOAD_NOTICES_SCRIPT,
            [
                "--db", str(out_db),
                "--raw", str(NOTICES_RAW_DIR),
                "--index", str(NOTICES_INDEX_PATH),
            ],
            description=f"load_notices.py(run {i + 1}/{runs})",
        )

        content_hash = _read_meta_value(out_db, "content_hash")
        hashes.append(content_hash)
        db_paths.append(out_db)
        elapsed = time.monotonic() - t0
        log(
            f"[생성 재현 {i + 1}/{runs}] 완료({elapsed:.1f}초) "
            f"content_hash={content_hash[:12] if content_hash else None}..."
        )

    mutual_identical, mismatch = compare_values(hashes)
    anchor_hash = read_anchor_hash()
    anchor_match = bool(hashes) and hashes[0] == anchor_hash

    result = {
        "runs": runs,
        # "identical"은 상호 일치뿐 아니라 봉인 앵커와의 일치도 요구한다(구현 판단 —
        # 재현 5회가 서로 같아도 전부 잘못된 값으로 같으면 재현성 검증의 목적을
        # 달성하지 못한다고 보았다. task-S23-report.md 구현 노트 참고).
        "identical": mutual_identical and anchor_match,
        "mutual_identical": mutual_identical,
        "hashes": hashes,
        "anchor_hash": anchor_hash,
        "anchor_match": anchor_match,
        "mismatch": mismatch,
    }
    return result, db_paths


# ---------------------------------------------------------------------------
# 계열 2 — 배치 재현
# ---------------------------------------------------------------------------


def run_batch_series(
    runs: int,
    source_db: Path,
    work_dir: Path,
    *,
    as_of: str = BATCH_AS_OF,
    params_path: str = DEFAULT_PARAMS_PATH,
    log: Callable[[str], None] = print,
) -> dict:
    """source_db(계열 1 산출 DB 1개)를 N개 사본으로 복제해 각각 run_risk_batch.py를 실행한다."""
    work_dir.mkdir(parents=True, exist_ok=True)
    run_ids: list[str] = []
    row_sets: list[list[tuple]] = []
    for i in range(runs):
        t0 = time.monotonic()
        log(f"[배치 재현 {i + 1}/{runs}] run_risk_batch.py 실행 중...")
        copy_path = work_dir / f"batch_{i}.db"
        shutil.copy2(source_db, copy_path)

        _run_script(
            RUN_RISK_BATCH_SCRIPT,
            ["--db", str(copy_path), "--as-of", as_of, "--params", params_path],
            description=f"run_risk_batch.py(run {i + 1}/{runs})",
        )

        run_id, rows = _read_risk_results(copy_path)
        run_ids.append(run_id)
        row_sets.append(rows)
        log(
            f"[배치 재현 {i + 1}/{runs}] 완료({time.monotonic() - t0:.1f}초) "
            f"run_id={run_id} 행수={len(rows)}"
        )

    run_id_identical, run_id_mismatch = compare_values(run_ids)
    rows_identical, rows_mismatch = compare_row_sets(row_sets)

    mismatch: dict | None = None
    if run_id_mismatch or rows_mismatch:
        mismatch = {}
        if run_id_mismatch:
            mismatch["run_id"] = run_id_mismatch
        if rows_mismatch:
            mismatch["rows"] = rows_mismatch

    return {
        "runs": runs,
        "identical": run_id_identical and rows_identical,
        "run_ids": run_ids,
        "row_count": len(row_sets[0]) if row_sets else 0,
        "digests": [sha256_of_json(sorted(rs)) for rs in row_sets],
        "mismatch": mismatch,
    }


# ---------------------------------------------------------------------------
# 계열 3 — 측정 재현
# ---------------------------------------------------------------------------


def run_detection_series(
    runs: int,
    db_path: Path,
    labels_path: str,
    start: str,
    end: str,
    work_dir: Path,
    *,
    params_path: str = DEFAULT_PARAMS_PATH,
    log: Callable[[str], None] = print,
) -> dict:
    """db_path(표준 스냅샷 원본, 읽기 전용)에 measure_detection.py를 동일 인자로 N회 실행한다.

    이 함수는 labels_path 문자열을 subprocess 인자로 그대로 전달할 뿐, 그 파일을 직접 열지
    않는다(위임 호출 — 모듈 docstring·isolation 등록 사유 참고). 실행 전후 db_path의
    바이트 해시를 비교해 "읽기 전용" 전제가 실제로 지켜졌는지 재확인한다.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    before_hash = _sha256_file(db_path)

    results_list: list[dict] = []
    for i in range(runs):
        t0 = time.monotonic()
        log(f"[측정 재현 {i + 1}/{runs}] measure_detection.py 실행 중...")
        out_path = work_dir / f"detection_{i}.json"

        _run_script(
            MEASURE_DETECTION_SCRIPT,
            [
                "--db", str(db_path),
                "--labels", labels_path,
                "--start", start,
                "--end", end,
                "--out", str(out_path),
                "--params", params_path,
            ],
            description=f"measure_detection.py(run {i + 1}/{runs})",
        )

        payload = json.loads(out_path.read_text(encoding="utf-8"))
        results_list.append(payload["results"])
        log(f"[측정 재현 {i + 1}/{runs}] 완료({time.monotonic() - t0:.1f}초)")

    after_hash = _sha256_file(db_path)
    db_unchanged = before_hash == after_hash
    if not db_unchanged:
        raise RuntimeError(
            "표준 스냅샷이 측정 재현 도중 변경됐다(읽기 전용 원칙 위반) — "
            f"before={before_hash[:12]}... after={after_hash[:12]}..."
        )

    identical, mismatch = compare_json_dicts(results_list, ignore_keys=_TIMESTAMP_KEYS)
    return {
        "runs": runs,
        "identical": identical,
        "digests": [sha256_of_json(r) for r in results_list],
        "db_unchanged": db_unchanged,
        "mismatch": mismatch,
    }


# ---------------------------------------------------------------------------
# 오케스트레이션
# ---------------------------------------------------------------------------


def run_verification(
    runs: int,
    out_path: str | Path,
    labels_path: str,
    detection_start: str,
    detection_end: str,
    *,
    params_path: str = DEFAULT_PARAMS_PATH,
    log: Callable[[str], None] = print,
) -> dict:
    """3계열을 순서대로 실행하고 결과 JSON을 out_path에 쓴다. 결과 dict를 반환한다."""
    with tempfile.TemporaryDirectory(prefix="verify-repro-") as tmp:
        tmp_path = Path(tmp)

        log(f"=== 1/3 생성 재현({runs}회) ===")
        generation, gen_db_paths = run_generation_series(runs, tmp_path / "generation", log=log)

        log(f"=== 2/3 배치 재현({runs}회) ===")
        batch = run_batch_series(
            runs, gen_db_paths[0], tmp_path / "batch", params_path=params_path, log=log
        )

        log(f"=== 3/3 측정 재현({runs}회) ===")
        detection = run_detection_series(
            runs,
            STANDARD_DB_PATH,
            labels_path,
            detection_start,
            detection_end,
            tmp_path / "detection",
            params_path=params_path,
            log=log,
        )

    verdict = compute_verdict(generation, batch, detection)

    result = {
        "measured_at": datetime.now().isoformat(timespec="seconds"),
        "runs": runs,
        "generation": generation,
        "batch": batch,
        "detection": detection,
        "verdict": verdict,
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
    }

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iso_date_str(value: str) -> str:
    """형식만 검증하고(ISO 날짜) 문자열 그대로 반환한다 — 하위 스크립트에 그대로 전달."""
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"ISO 날짜(YYYY-MM-DD)여야 한다: {value!r}") from exc
    return value


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar subprocess 수준 재현성 검증(생성·배치·측정 3계열, 각 N회)"
    )
    parser.add_argument("--runs", type=int, default=5, help="계열별 반복 횟수(기본 5)")
    parser.add_argument("--out", required=True, help="결과 JSON 출력 경로")
    parser.add_argument(
        "--labels",
        required=True,
        help=(
            "측정 재현(계열 3)에서 measure_detection.py subprocess에 그대로 전달할 정답"
            " 라벨 JSON 경로 — 이 스크립트 자신은 그 파일을 열지 않는다(위임 호출)"
        ),
    )
    parser.add_argument(
        "--detection-start",
        dest="detection_start",
        type=_iso_date_str,
        required=True,
        metavar="YYYY-MM-DD",
        help="측정 재현 스윕 시작일(measure_detection.py --start로 그대로 전달)",
    )
    parser.add_argument(
        "--detection-end",
        dest="detection_end",
        type=_iso_date_str,
        required=True,
        metavar="YYYY-MM-DD",
        help="측정 재현 스윕 종료일(measure_detection.py --end로 그대로 전달)",
    )
    parser.add_argument(
        "--params",
        default=DEFAULT_PARAMS_PATH,
        help=f"배치·측정 재현에 공통으로 쓸 분석 파라미터 TOML 경로(기본: {DEFAULT_PARAMS_PATH})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    result = run_verification(
        args.runs,
        args.out,
        args.labels,
        args.detection_start,
        args.detection_end,
        params_path=args.params,
    )

    print(f"verdict: {result['verdict']}")
    print(
        f"생성 재현: identical={result['generation']['identical']} "
        f"(상호일치={result['generation']['mutual_identical']}, "
        f"앵커일치={result['generation']['anchor_match']})"
    )
    print(f"배치 재현: identical={result['batch']['identical']}")
    print(f"측정 재현: identical={result['detection']['identical']}")
    print(f"결과 저장: {args.out}")

    return 0 if result["verdict"] else 1


if __name__ == "__main__":
    sys.exit(main())
