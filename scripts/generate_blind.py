"""CLI 진입점 — 블라인드 평가 스냅샷 생성기(Task S-22).

사용법(M-30 표준 호출 형태):
    python scripts/generate_blind.py --ranges data/scenarios/blind_ranges.yaml \
        --seed 20260901 --base-date 2026-08-01 --out data/blind/blind_20260901.db

범위 YAML(--ranges)에서 --seed로 결정적 파라미터를 뽑아(품목당 유형 1개) 표준 생성
경로(scripts/datagen/baseline·inject·labels·config)를 그대로 재사용해 스냅샷을 만든다.
라벨은 즉시 봉인 규약에 따라 data/blind/sealed/에 분리 저장하고, data/blind/manifest.json에
{db 파일명, db sha256, labels 파일명, labels sha256, seed, 생성 파라미터 요약}을 기록한다.
out DB에는 라벨·시나리오 흔적이 전혀 남지 않는다.

실제 생성·봉인 로직은 scripts/datagen/blind.py에 있다 — 이 파일은 얇은 CLI 진입점이며,
scripts/generate_dataset.py와 마찬가지로 medsupply 패키지를 import하지 않는다.

생성된 스냅샷의 정합성은 scripts/validate_dataset.py로 독립 검증한다(check 10은 스냅샷
자체 해시 기준 — 블라인드는 매 seed마다 content_hash가 달라 표준의 --expect-hash 방식이
적용되지 않는다).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.*`를 절대 경로 실행에서도 import할 수
# 있게 한다(generate_dataset.py와 동일한 방식).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen import blind  # noqa: E402


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="MedSupply Radar 블라인드 평가 스냅샷 생성기(범위 YAML + seed → 결정적 1건)"
    )
    parser.add_argument(
        "--ranges", default=str(blind.DEFAULT_RANGES_PATH), help="파라미터 범위 YAML 경로"
    )
    parser.add_argument("--seed", required=True, type=int, help="결정적 생성 시드")
    parser.add_argument("--base-date", required=True, help="기준일(YYYY-MM-DD)")
    parser.add_argument("--out", required=True, help="출력 SQLite 파일 경로")
    parser.add_argument(
        "--sealed-dir",
        default=str(blind.DEFAULT_SEALED_DIR),
        help="봉인 라벨 출력 디렉터리(기본 data/blind/sealed)",
    )
    parser.add_argument(
        "--manifest",
        default=str(blind.DEFAULT_MANIFEST_PATH),
        help="봉인 매니페스트 경로(기본 data/blind/manifest.json, append/upsert)",
    )
    return parser


def main(argv: list[str] | None = None) -> blind.BlindResult:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    result = blind.generate_blind(
        args.ranges,
        args.seed,
        args.base_date,
        args.out,
        sealed_dir=args.sealed_dir,
        manifest_path=args.manifest,
    )

    print(f"seed: {result.seed}")
    print(f"품목 수: {result.summary.item_count}")
    print(f"시계열 행 수: {result.summary.timeseries_row_count}")
    print(f"발주 건수: {result.summary.shipment_count}")
    print(f"content_hash: {result.summary.content_hash}")
    print(f"db: {result.db_path} (sha256={result.db_sha256})")
    print(f"labels(sealed): {result.labels_path} (sha256={result.labels_sha256})")
    print(f"지연 도착 arm 포함: {result.has_delayed_arrival}")
    print(f"재시도 횟수: {result.attempts_used}")

    return result


if __name__ == "__main__":
    main()
