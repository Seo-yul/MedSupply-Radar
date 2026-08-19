"""CLI 진입점 — 결정적 데이터셋 생성기.

사용법(베이스라인만):
    python scripts/generate_dataset.py --out data/medsupply.db --seed 20260801 \
        --base-date 2026-08-01 --baseline-only

사용법(시나리오 20건 주입 + 라벨 산출 + 대응 이력 시드 적재 — 표준 스냅샷 전체 빌드):
    python scripts/generate_dataset.py --config data/scenarios/scenario_config.yaml \
        --out data/medsupply.db --seed 20260801 --base-date 2026-08-01 \
        --labels-out data/scenarios/ground_truth/standard_v1.json

--baseline-only 없이 실행하면 scripts/datagen/inject.py의 주입 경로가 활성화되고, 뒤이어
data/reference/action_history_seed.csv(8건)를 scripts/datagen/inject.py의
load_action_history_seed로 action_history에 적재한다(--skip-history-seed로 옵트아웃
가능 — 이력 시드 적재는 "전체 빌드"인 주입 경로에서만 의미가 있고, --baseline-only는
애초에 이 단계를 거치지 않는다). 이력 시드 적재는 inject_scenarios가 이미 content_hash를
계산해 meta에 저장한 뒤에 일어나므로, 적재 직후 content_hash를 재계산해 meta에 다시
저장한다(apply_history_seed) — 그래야 완성 스냅샷에서 scripts/validate_dataset.py가
재계산한 값과 meta.content_hash가 항상 일치한다.

--config는 시나리오 config 경로(기본 data/scenarios/scenario_config.yaml)이며 --baseline-only
에서는 여전히 무시한다. --labels-out을 주면 도출된 라벨(20건) JSON을 그 경로에 쓴다(주입
경로에서만 의미 있음 — --baseline-only와 함께 주면 무시된다).

실제 생성 로직은 scripts/datagen/baseline.py(베이스라인)·scripts/datagen/inject.py(주입 +
라벨 + 이력 시드 적재 함수)에 있다 — 이 파일은 얇은 CLI 진입점이며, scripts/datagen/과
마찬가지로 medsupply 패키지를 import하지 않는다.

생성된 스냅샷의 정합성은 scripts/validate_dataset.py로 독립 검증한다(10항목, PASS/WARN/FAIL).
"""

from __future__ import annotations

import dataclasses
import sqlite3
import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.*`를 절대 경로 실행에서도 import할 수
# 있게 한다("python scripts/generate_dataset.py"로 직접 실행하면 sys.path[0]이 scripts/가
# 되어 리포 루트가 기본으로는 잡히지 않는다).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen.baseline import (  # noqa: E402
    DEFAULT_REFERENCE_DIR,
    GenerationSummary,
    _build_arg_parser,
    compute_content_hash,
    generate_baseline,
)
from scripts.datagen.inject import (  # noqa: E402
    DEFAULT_SCENARIO_CONFIG_PATH,
    inject_scenarios,
    load_action_history_seed,
)
from scripts.datagen.labels import labels_to_json  # noqa: E402

#: action_history_seed.csv 기본 경로(S-12 산출물, 8건 고정).
DEFAULT_ACTION_HISTORY_SEED_PATH = DEFAULT_REFERENCE_DIR / "action_history_seed.csv"


def apply_history_seed(
    out_path: str | Path,
    *,
    csv_path: str | Path = DEFAULT_ACTION_HISTORY_SEED_PATH,
    skip: bool = False,
) -> tuple[int, str | None]:
    """대응 이력 시드를 적재하고 content_hash를 재계산해 meta에 갱신한다.

    generate_baseline·inject_scenarios는 둘 다 out_path 파일을 삭제 후 재생성하므로, 이
    함수는 반드시 그 함수들이 끝난 뒤에 호출해야 한다(먼저 실행하면 결과가 사라진다). 두
    함수 모두 자신이 만든 데이터만으로 content_hash를 계산해 meta에 저장한 채 반환하므로,
    이 함수가 action_history_seed.csv를 추가로 적재하면(skip=False) 그만큼 데이터가 늘어나
    기존 content_hash가 낡은 값이 된다 — 그래서 적재 직후 재계산해 meta.content_hash를
    갱신한다. 그래야 validate_dataset.py의 "재계산 == meta.content_hash" 검사가 항상
    성립한다.

    skip=True면 아무 것도 하지 않고 (0, None)을 반환한다(호출부가 기존 summary.content_hash를
    그대로 쓰라는 신호). 반환값은 (적재 건수, 갱신된 content_hash 또는 None).
    """
    if skip:
        return 0, None

    out_path = Path(out_path)
    conn = sqlite3.connect(out_path)
    try:
        count = load_action_history_seed(conn, csv_path)
        new_hash = compute_content_hash(conn)
        with conn:
            conn.execute("UPDATE meta SET value = ? WHERE key = 'content_hash'", (new_hash,))
    finally:
        conn.close()
    return count, new_hash


def main(argv: list[str] | None = None) -> GenerationSummary:
    parser = _build_arg_parser()
    parser.add_argument(
        "--labels-out",
        default=None,
        help="라벨(JSON, 20건) 출력 경로 — 주입 경로(--baseline-only 아닐 때)에서만 사용",
    )
    parser.add_argument(
        "--skip-history-seed",
        action="store_true",
        help=(
            "대응 이력 시드(action_history_seed.csv) 적재를 건너뛴다"
            "(주입 경로에서만 의미 있음 — --baseline-only는 애초에 이력 시드를 적재하지 않는다)"
        ),
    )
    args = parser.parse_args(argv)

    if args.baseline_only:
        summary = generate_baseline(args.out, seed=args.seed, base_date=args.base_date)
        history_seed_count = 0
    else:
        config_path = args.config or DEFAULT_SCENARIO_CONFIG_PATH
        summary, labels = inject_scenarios(
            args.out,
            seed=args.seed,
            base_date=args.base_date,
            scenario_config_path=config_path,
        )
        if args.labels_out:
            labels_out = Path(args.labels_out)
            labels_out.parent.mkdir(parents=True, exist_ok=True)
            labels_out.write_text(labels_to_json(labels), encoding="utf-8")

        history_seed_count, refreshed_hash = apply_history_seed(
            args.out, skip=args.skip_history_seed
        )
        if refreshed_hash is not None:
            summary = dataclasses.replace(summary, content_hash=refreshed_hash)

    print(f"품목 수: {summary.item_count}")
    print(f"시계열 행 수: {summary.timeseries_row_count}")
    print(f"발주 건수: {summary.shipment_count}")
    print(f"절삭 카운터: {summary.truncation_count}")
    print(f"이력 시드 건수: {history_seed_count}")
    print(f"content_hash: {summary.content_hash}")
    print(f"실행 시간: {summary.elapsed_seconds:.2f}초")

    return summary


if __name__ == "__main__":
    main()
