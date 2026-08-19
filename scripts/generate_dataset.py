"""CLI 진입점 — 결정적 데이터셋 생성기.

사용법(베이스라인만):
    python scripts/generate_dataset.py --out data/medsupply.db --seed 20260801 \
        --base-date 2026-08-01 --baseline-only

사용법(시나리오 20건 주입 + 라벨 산출):
    python scripts/generate_dataset.py --out data/medsupply.db --seed 20260801 \
        --base-date 2026-08-01 --labels-out data/scenarios/ground_truth/standard_v1.json

--baseline-only 없이 실행하면 scripts/datagen/inject.py의 주입 경로가 활성화된다(S-12
이전에는 이 경로가 "시나리오 주입은 미구현(S-12)" 에러로 종료됐다). --config는 시나리오
config 경로(기본 data/scenarios/scenario_config.yaml)이며 --baseline-only에서는 여전히
무시한다. --labels-out을 주면 도출된 라벨(20건) JSON을 그 경로에 쓴다(주입 경로에서만
의미 있음 — --baseline-only와 함께 주면 무시된다).

실제 생성 로직은 scripts/datagen/baseline.py(베이스라인)·scripts/datagen/inject.py(주입 +
라벨)에 있다 — 이 파일은 얇은 CLI 진입점이며, scripts/datagen/과 마찬가지로 medsupply
패키지를 import하지 않는다.

표준 스냅샷 빌드(정합성·결정성 검증, 해시 기록 등 CLI 통합의 나머지)는 S-13 소관이다.
"""

from __future__ import annotations

import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.*`를 절대 경로 실행에서도 import할 수
# 있게 한다("python scripts/generate_dataset.py"로 직접 실행하면 sys.path[0]이 scripts/가
# 되어 리포 루트가 기본으로는 잡히지 않는다).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen.baseline import GenerationSummary, _build_arg_parser, generate_baseline  # noqa: E402
from scripts.datagen.inject import DEFAULT_SCENARIO_CONFIG_PATH, inject_scenarios  # noqa: E402
from scripts.datagen.labels import labels_to_json  # noqa: E402


def main(argv: list[str] | None = None) -> GenerationSummary:
    parser = _build_arg_parser()
    parser.add_argument(
        "--labels-out",
        default=None,
        help="라벨(JSON, 20건) 출력 경로 — 주입 경로(--baseline-only 아닐 때)에서만 사용",
    )
    args = parser.parse_args(argv)

    if args.baseline_only:
        summary = generate_baseline(args.out, seed=args.seed, base_date=args.base_date)
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

    print(f"품목 수: {summary.item_count}")
    print(f"시계열 행 수: {summary.timeseries_row_count}")
    print(f"발주 건수: {summary.shipment_count}")
    print(f"절삭 카운터: {summary.truncation_count}")
    print(f"content_hash: {summary.content_hash}")
    print(f"실행 시간: {summary.elapsed_seconds:.2f}초")

    return summary


if __name__ == "__main__":
    main()
