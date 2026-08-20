"""공고 원문 적재 스크립트 — data/notices/raw/*.txt + notices_index.csv → notices 테이블.

**적재 계층 규약**: 이 스크립트는 부트스트랩 로더다. scripts/datagen/baseline.py·
scripts/datagen/inject.py와 동급으로, medsupply/data/writer.py(쓰기 단일 경로)를
경유하지 않고 raw SQL로 직접 INSERT한다 — medsupply 패키지도 일절 import하지 않는다.
"표준 스냅샷"의 정의가 "데이터셋 생성 + 공고 적재 완료" 상태로 확장되면서, 표준 빌드
시퀀스는 다음 두 명령의 순서로 이어진다.

    1) python scripts/generate_dataset.py --config data/scenarios/scenario_config.yaml \
           --out data/medsupply.db --seed 20260801 --base-date 2026-08-01
    2) python scripts/load_notices.py --db data/medsupply.db --raw data/notices/raw \
           --index data/notices/notices_index.csv

**raw 파일 파싱 규칙**: raw/*.txt의 처음 3줄은 헤더(`# source: <URL>`,
`# collected_at: <ISO8601>`, `# notice_type: <값>`)이고, 4번째 줄부터 파일 끝까지가
raw_text다(원문 무가공 — text.split("\\n", 3)의 4번째 조각을 그대로 쓴다. splitlines로
줄 단위 재구성을 하지 않으므로 개행·후행 공백까지 원문과 바이트 단위로 같다). 헤더의
notice_type과 notices_index.csv 해당 행의 notice_type이 다르면 ValueError로 실패한다.
title·published_date·source(기관명)는 헤더에 없는 필드라 색인 CSV에서만 채운다.

**notice_id**: 파일명의 NNN 접두(예: "009_...") → "N-009" 형식.

**정합 강제**: notices_index.csv에 있는 파일이 raw 디렉터리에 없거나, raw 디렉터리에
있는 파일이 색인에 없으면(orphan) ValueError로 실패한다(data/scenarios·ground_truth는
참조하지 않는다 — 이 정합 검사만으로 충분하다).

**멱등성**: INSERT OR REPLACE(notice_id PK)라 재실행해도 행 수·값은 그대로다. 다만
매 실행마다 meta.data_version을 1 증가시키고 meta.content_hash를 재계산해 갱신한다
(scripts.datagen.baseline.compute_content_hash 재사용). 갱신 순서가 중요하다 —
content_hash는 meta 테이블 전체(자기 자신 제외)를 직렬화하므로, data_version을 먼저
갱신한 뒤 content_hash를 "가장 마지막에" 계산·저장해야 재계산 시 자기정합이 성립한다.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# 리포 루트를 sys.path에 올려 `scripts.datagen.*`를 절대 경로 실행에서도 import할 수
# 있게 한다(scripts/generate_dataset.py와 동일한 방식).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.datagen.baseline import compute_content_hash  # noqa: E402

_HEADER_LINE_COUNT = 3
_SOURCE_PREFIX = "# source: "
_COLLECTED_AT_PREFIX = "# collected_at: "
_NOTICE_TYPE_PREFIX = "# notice_type: "


@dataclass(frozen=True)
class LoadSummary:
    """load_notices()의 반환값 — CLI 요약 출력 및 테스트 검증에 쓰인다."""

    loaded_count: int
    notice_type_counts: dict[str, int]
    content_hash: str


def _read_index(index_path: Path) -> list[dict[str, str]]:
    with index_path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _check_file_alignment(index_rows: list[dict[str, str]], raw_dir: Path) -> None:
    """색인 ↔ raw 디렉터리 정합을 양방향으로 강제한다(둘 중 하나라도 어긋나면 에러)."""
    indexed = {row["file"] for row in index_rows}
    on_disk = {p.name for p in raw_dir.glob("*.txt")}

    missing_on_disk = sorted(indexed - on_disk)
    if missing_on_disk:
        raise ValueError(f"색인에는 있지만 raw 디렉터리에 없는 파일: {missing_on_disk}")

    orphans = sorted(on_disk - indexed)
    if orphans:
        raise ValueError(f"raw 디렉터리에는 있지만 색인에 없는 파일(orphan): {orphans}")


def _parse_raw_file(path: Path) -> tuple[str, str, str, str]:
    """raw txt 파일을 파싱해 (source_url, collected_at, notice_type, raw_text)를 반환한다."""
    text = path.read_text(encoding="utf-8")
    parts = text.split("\n", _HEADER_LINE_COUNT)
    if len(parts) < _HEADER_LINE_COUNT + 1:
        raise ValueError(f"{path.name}: 헤더 {_HEADER_LINE_COUNT}줄 + 본문 형식이 아니다")

    source_line, collected_line, type_line, raw_text = parts

    if not source_line.startswith(_SOURCE_PREFIX):
        raise ValueError(f"{path.name}: 1행이 {_SOURCE_PREFIX!r}로 시작하지 않는다")
    if not collected_line.startswith(_COLLECTED_AT_PREFIX):
        raise ValueError(f"{path.name}: 2행이 {_COLLECTED_AT_PREFIX!r}로 시작하지 않는다")
    if not type_line.startswith(_NOTICE_TYPE_PREFIX):
        raise ValueError(f"{path.name}: 3행이 {_NOTICE_TYPE_PREFIX!r}로 시작하지 않는다")

    source_url = source_line[len(_SOURCE_PREFIX) :].strip()
    collected_at = collected_line[len(_COLLECTED_AT_PREFIX) :].strip()
    notice_type = type_line[len(_NOTICE_TYPE_PREFIX) :].strip()
    return source_url, collected_at, notice_type, raw_text


def _notice_id_from_filename(filename: str) -> str:
    """파일명의 NNN 접두(예: '009_...') → 'N-009' 형식."""
    return f"N-{filename[:3]}"


def _bump_data_version(conn: sqlite3.Connection) -> None:
    """meta.data_version을 정수 증가시킨다(없으면 1로 생성).

    medsupply/data/writer.py의 _bump_data_version과 동일한 규약이지만, 이 스크립트는
    부트스트랩 로더라 medsupply를 import하지 않고 자체 구현한다.
    """
    row = conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta(key, value) VALUES ('data_version', '1')")
    else:
        conn.execute(
            "UPDATE meta SET value = ? WHERE key = 'data_version'",
            (str(int(row[0]) + 1),),
        )


def load_notices(
    conn: sqlite3.Connection, raw_dir: str | Path, index_path: str | Path
) -> LoadSummary:
    """notices_index.csv를 읽어 raw_dir의 원문 파일을 notices 테이블에 적재한다.

    INSERT OR REPLACE(notice_id PK)로 멱등하게 적재한다. 적재 전체가 하나의 트랜잭션
    으로 원자적이다 — 도중에 ValueError가 나면 그 실행에서 처리된 행은 하나도 커밋되지
    않는다. 적재 성공 후 meta.data_version을 1 증가시키고, meta.content_hash를
    재계산해 갱신한다(content_hash 계산은 항상 마지막 — 갱신된 data_version까지 반영된
    상태를 직렬화해야 재계산 자기정합이 성립한다).
    """
    raw_dir = Path(raw_dir)
    index_path = Path(index_path)

    index_rows = _read_index(index_path)
    _check_file_alignment(index_rows, raw_dir)

    type_counts: Counter[str] = Counter()

    with conn:
        for row in index_rows:
            filename = row["file"]
            source_url, collected_at, header_notice_type, raw_text = _parse_raw_file(
                raw_dir / filename
            )

            index_notice_type = row["notice_type"]
            if header_notice_type != index_notice_type:
                raise ValueError(
                    f"{filename}: 헤더 notice_type({header_notice_type!r}) != 색인"
                    f" notice_type({index_notice_type!r})"
                )

            notice_id = _notice_id_from_filename(filename)

            conn.execute(
                "INSERT OR REPLACE INTO notices(notice_id, published_date, title, source,"
                " source_url, raw_text, notice_type, collected_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    notice_id,
                    row["published_date"],
                    row["title"],
                    row["source"],
                    source_url,
                    raw_text,
                    index_notice_type,
                    collected_at,
                ),
            )
            type_counts[index_notice_type] += 1

        _bump_data_version(conn)
        content_hash = compute_content_hash(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('content_hash', ?)",
            (content_hash,),
        )

    return LoadSummary(
        loaded_count=sum(type_counts.values()),
        notice_type_counts=dict(type_counts),
        content_hash=content_hash,
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="공고 원문(raw/*.txt + notices_index.csv)을 notices 테이블에 적재한다"
    )
    parser.add_argument("--db", required=True, help="적재 대상 SQLite DB 경로")
    parser.add_argument("--raw", required=True, help="공고 원문 디렉터리(raw/*.txt)")
    parser.add_argument("--index", required=True, help="공고 색인 CSV 경로(notices_index.csv)")
    return parser


def main(argv: list[str] | None = None) -> LoadSummary:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        summary = load_notices(conn, raw_dir=args.raw, index_path=args.index)
    finally:
        conn.close()

    print(f"적재 건수: {summary.loaded_count}")
    print(f"notice_type 분포: {summary.notice_type_counts}")
    print(f"content_hash: {summary.content_hash}")

    return summary


if __name__ == "__main__":
    main()
