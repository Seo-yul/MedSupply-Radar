"""scripts/load_notices.py(공고 원문 적재) 계약 검증.

data/notices/raw/*.txt + notices_index.csv를 notices 테이블에 적재하는 부트스트랩
로더의 계약을 고정한다: 건수 정합, notice_id 형식, raw_text 원문 보존(헤더 미포함),
날짜 ISO 포맷, 멱등성(재적재해도 데이터 불변·data_version만 증가), content_hash
자기정합(F2 회귀 방지), 색인↔raw 파일 정합 위반·헤더 notice_type 불일치 시 명확한
에러.

실제 data/notices/ 산출물(M-09·M-10 수집물, 20건)을 그대로 입력으로 쓴다 — 이
스크립트가 그 실물을 실제로 적재할 수 있어야 하는 계약이라 모의 데이터로는 검증되지
않는다. 훼손 케이스는 tmp_path에 raw 디렉터리를 복사해 변형한다(원본 data/notices/raw
는 읽기 전용으로 남긴다).
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from scripts import load_notices
from scripts.datagen.baseline import apply_schema, compute_content_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "data" / "notices" / "raw"
INDEX_CSV = REPO_ROOT / "data" / "notices" / "notices_index.csv"

EXPECTED_NOTICE_COUNT = 20
SAMPLE_FILE = "001_2024-10-17_아지트로마이신_아지탑스주사_공급부족.txt"
SAMPLE_NOTICE_ID = "N-001"
ALLOWED_NOTICE_TYPES = {"공급중단", "공급부족", "정상화", "기타"}

COLLECTED_AT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")

NOTICE_ROW_COLUMNS = (
    "notice_id, published_date, title, source, source_url, raw_text, notice_type,"
    " collected_at"
)


def _build_schema_only_db(path: Path) -> None:
    """schema.sql만 적용하고 표준 스냅샷 완성 직후(generate_dataset.py 실행 직후)와
    동일한 형태로 meta 6키 + content_hash를 채운 tmp DB를 만든다. load_notices.py는
    이 상태를 이어받아 실행되는 것이 전제다(브리프: 표준 빌드 시퀀스 generate →
    load_notices)."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    try:
        apply_schema(conn)
        with conn:
            conn.executemany(
                "INSERT INTO meta(key, value) VALUES (?, ?)",
                [
                    ("seed", "999"),
                    ("base_date", "2026-08-01"),
                    ("item_count", "0"),
                    ("generated_at", "2026-08-01T09:30:00"),
                    ("data_version", "1"),
                    ("config_hash", "test-config-hash"),
                ],
            )
        content_hash = compute_content_hash(conn)
        with conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES ('content_hash', ?)", (content_hash,)
            )
    finally:
        conn.close()


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "notices_test.db"
    _build_schema_only_db(path)
    return path


@pytest.fixture()
def tmp_raw_dir(tmp_path: Path) -> Path:
    """실제 raw 디렉터리의 복사본(훼손 케이스 테스트가 원본을 건드리지 않도록)."""
    dest = tmp_path / "raw_copy"
    shutil.copytree(RAW_DIR, dest)
    return dest


# ---------------------------------------------------------------------------
# 정상 적재
# ---------------------------------------------------------------------------


class TestLoadNotices:
    def test_loaded_count_matches_index_row_count(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            summary = load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            count = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        finally:
            conn.close()
        assert summary.loaded_count == EXPECTED_NOTICE_COUNT
        assert count == EXPECTED_NOTICE_COUNT

    def test_notice_id_format(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            ids = [
                r[0] for r in conn.execute("SELECT notice_id FROM notices ORDER BY notice_id")
            ]
        finally:
            conn.close()
        assert SAMPLE_NOTICE_ID in ids
        for notice_id in ids:
            assert re.match(r"^N-\d{3}$", notice_id), notice_id

    def test_raw_text_excludes_header_and_preserves_original(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            row = conn.execute(
                "SELECT raw_text FROM notices WHERE notice_id = ?", (SAMPLE_NOTICE_ID,)
            ).fetchone()
        finally:
            conn.close()
        raw_text = row[0]

        original = (RAW_DIR / SAMPLE_FILE).read_text(encoding="utf-8")
        expected_body = original.split("\n", 3)[3]

        assert raw_text == expected_body
        assert not raw_text.lstrip().startswith("#")
        assert "[공급중단·부족의약품 기본정보]" in raw_text

    def test_collected_at_and_published_date_are_iso(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            rows = conn.execute("SELECT published_date, collected_at FROM notices").fetchall()
        finally:
            conn.close()
        assert rows
        for published_date, collected_at in rows:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", published_date)
            date.fromisoformat(published_date)
            assert COLLECTED_AT_PATTERN.match(collected_at), collected_at

    def test_notice_type_distribution_sums_to_total_and_is_allowed(
        self, db_path: Path
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            summary = load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
        finally:
            conn.close()
        assert sum(summary.notice_type_counts.values()) == EXPECTED_NOTICE_COUNT
        assert set(summary.notice_type_counts) <= ALLOWED_NOTICE_TYPES

    def test_source_and_title_come_from_index_csv(self, db_path: Path) -> None:
        """notices.source(기관명)·title은 raw 헤더에 없는 필드라 색인 CSV에서 와야 한다."""
        conn = sqlite3.connect(db_path)
        try:
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            row = conn.execute(
                "SELECT source, source_url, title FROM notices WHERE notice_id = ?",
                (SAMPLE_NOTICE_ID,),
            ).fetchone()
        finally:
            conn.close()
        source, source_url, title = row
        assert source == "의약품안전나라"
        assert source_url.startswith("https://nedrug.mfds.go.kr/")
        assert "아지탑스주사" in title


# ---------------------------------------------------------------------------
# 멱등성
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_rerun_keeps_row_count_and_values_but_bumps_data_version(
        self, db_path: Path
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            first_rows = conn.execute(
                f"SELECT {NOTICE_ROW_COLUMNS} FROM notices ORDER BY notice_id"
            ).fetchall()
            version_after_first = int(
                conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()[0]
            )

            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            second_rows = conn.execute(
                f"SELECT {NOTICE_ROW_COLUMNS} FROM notices ORDER BY notice_id"
            ).fetchall()
            version_after_second = int(
                conn.execute("SELECT value FROM meta WHERE key = 'data_version'").fetchone()[0]
            )
        finally:
            conn.close()

        assert len(first_rows) == len(second_rows) == EXPECTED_NOTICE_COUNT
        assert first_rows == second_rows
        assert version_after_second == version_after_first + 1


# ---------------------------------------------------------------------------
# content_hash 자기정합(F2 회귀 방지)
# ---------------------------------------------------------------------------


class TestContentHashSelfConsistency:
    def test_recomputed_hash_matches_stored_meta_value(self, db_path: Path) -> None:
        conn = sqlite3.connect(db_path)
        try:
            summary = load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            stored = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
            recomputed = compute_content_hash(conn)
        finally:
            conn.close()
        assert stored == recomputed == summary.content_hash

    def test_hash_changes_after_load_since_notices_are_source_data(self, db_path: Path) -> None:
        """공고 적재 후 content_hash가 바뀐다 — notices가 **부트스트랩 원천**이기 때문이다.

        갱신 사유(S-17 리뷰 F1): 원래 이름·근거는 "data_version이 포함되므로"였는데,
        새 범위에서 meta는 제외됐다. 해시가 바뀌는 진짜 이유는 notices 행이 늘어서다
        (단언은 그대로 — 근거 서술만 실제와 맞춘다).
        """
        conn = sqlite3.connect(db_path)
        try:
            pre_hash = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
            load_notices.load_notices(conn, raw_dir=RAW_DIR, index_path=INDEX_CSV)
            post_hash = conn.execute(
                "SELECT value FROM meta WHERE key = 'content_hash'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert pre_hash != post_hash


# ---------------------------------------------------------------------------
# 훼손 케이스
# ---------------------------------------------------------------------------


class TestCorruptionDetection:
    def test_orphan_raw_file_not_in_index_raises(self, db_path: Path, tmp_raw_dir: Path) -> None:
        (tmp_raw_dir / "999_2099-01-01_orphan_공급중단.txt").write_text(
            "# source: https://example.invalid/orphan\n"
            "# collected_at: 2026-08-19T00:00:00Z\n"
            "# notice_type: 공급중단\n"
            "\n본문\n",
            encoding="utf-8",
        )
        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(ValueError):
                load_notices.load_notices(conn, raw_dir=tmp_raw_dir, index_path=INDEX_CSV)
        finally:
            conn.close()

    def test_index_row_missing_raw_file_raises(self, db_path: Path, tmp_raw_dir: Path) -> None:
        (tmp_raw_dir / SAMPLE_FILE).unlink()
        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(ValueError):
                load_notices.load_notices(conn, raw_dir=tmp_raw_dir, index_path=INDEX_CSV)
        finally:
            conn.close()

    def test_header_notice_type_mismatch_raises(self, db_path: Path, tmp_raw_dir: Path) -> None:
        target = tmp_raw_dir / SAMPLE_FILE
        text = target.read_text(encoding="utf-8")
        mutated = text.replace("# notice_type: 공급부족", "# notice_type: 공급중단", 1)
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")

        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(ValueError):
                load_notices.load_notices(conn, raw_dir=tmp_raw_dir, index_path=INDEX_CSV)
        finally:
            conn.close()

    def test_corruption_does_not_partially_commit(self, db_path: Path, tmp_raw_dir: Path) -> None:
        """훼손 케이스에서 예외가 나도 이미 처리된 앞선 행이 커밋되어 남지 않아야 한다."""
        target = tmp_raw_dir / SAMPLE_FILE
        text = target.read_text(encoding="utf-8")
        target.write_text(
            text.replace("# notice_type: 공급부족", "# notice_type: 공급중단", 1),
            encoding="utf-8",
        )

        conn = sqlite3.connect(db_path)
        try:
            with pytest.raises(ValueError):
                load_notices.load_notices(conn, raw_dir=tmp_raw_dir, index_path=INDEX_CSV)
            count = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        finally:
            conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# CLI 계약 스모크 — python scripts/load_notices.py --db --raw --index
# ---------------------------------------------------------------------------


class TestCli:
    def test_main_wires_db_raw_index_flags(self, db_path: Path) -> None:
        summary = load_notices.main(
            ["--db", str(db_path), "--raw", str(RAW_DIR), "--index", str(INDEX_CSV)]
        )
        assert summary.loaded_count == EXPECTED_NOTICE_COUNT

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute("SELECT COUNT(*) FROM notices").fetchone()[0]
        finally:
            conn.close()
        assert count == EXPECTED_NOTICE_COUNT
