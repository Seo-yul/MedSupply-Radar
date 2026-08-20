"""저장소 전역 격리 가드 — "로직이 시나리오·정답을 볼 수 없다"는 검증 객관성 원칙(마스터
플랜 결정 4)을 정적으로 기계 강제한다(Task M-19).

검사는 두 방향이다.
- 순방향(로직→정답 차단): medsupply/ 전체 + app.py(+ scripts/의 지정 파일 일부)가 시나리오
  생성기·측정 스크립트를 임포트하거나, 정답 경로(data/scenarios·ground_truth)를 코드 값으로
  쓰지 않는다.
- 역방향(생성기→로직 차단): scripts/datagen/ 전체가 medsupply를 임포트하지 않는다.

"문자열 grep"이 아니라 ast 기반 임포트문·문자열 상수 검사다 — docstring·주석 서술(예:
medsupply/settings.py:4의 원칙 서술)은 허용해야 하므로, 검사 함수는 "docstring 위치가 아닌
문자열 상수"만 코드 값으로 취급한다. ast 규칙: Expr 단독 문장(예: 모듈/함수 docstring)의
문자열은 서술용으로 간주해 제외하고, 주석은 애초에 ast에 나타나지 않으므로 자연히 제외된다.

검사 함수는 전부 "파일 목록을 인자로 받는 순수 함수"다(디렉터리 순회는 호출부 책임) — 그래서
tmp_path에 만든 위반 샘플로 자가 검증할 수 있다(아래 "자가 검증" 절).
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "medsupply" / "data" / "schema.sql"

#: 순방향 임포트 금지 대상 — 시나리오 생성기·감지 측정 계열 전부(하위 모듈 포함, 브리프 §1).
FORBIDDEN_IMPORT_PREFIXES = ("scripts.datagen", "scripts.measure_detection")

#: 순방향 경로 리터럴 금지 마커 — 시나리오 입력/ground truth 산출 경로(브리프 §1).
PATH_LITERAL_MARKERS = ("data/scenarios", "ground_truth")


# ---------------------------------------------------------------------------
# 검사 함수(순수 함수 — 파일 목록을 인자로 받는다. 디렉터리 순회는 호출부 책임)
# ---------------------------------------------------------------------------


def _import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    """Import/ImportFrom 노드가 참조하는 모듈 이름들. ImportFrom은 상대 임포트(level>0)를
    이름 없는 모듈("")로 취급해 절대 경로 prefix와 우연히 일치하지 않게 한다."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level > 0:
        return [""]
    return [node.module or ""]


def _matches_forbidden_prefix(module_name: str, prefixes: tuple[str, ...]) -> bool:
    """module_name이 prefixes 중 하나와 정확히 같거나, 그 하위 모듈(prefix + '.')인지."""
    return any(module_name == prefix or module_name.startswith(prefix + ".") for prefix in prefixes)


def find_forbidden_imports(files: list[Path], forbidden_prefixes: tuple[str, ...]) -> list[str]:
    """files 각각을 ast로 파싱해 forbidden_prefixes(또는 그 하위 모듈)를 임포트하는 지점을
    찾는다. ast.Import/ast.ImportFrom만 본다(동적 임포트·문자열 조작은 대상 밖).

    Returns:
        위반 설명 문자열 리스트("path:lineno: import 문 요약"). 비어 있으면 위반 없음.
    """
    violations: list[str] = []
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for module_name in _import_names(node):
                if module_name and _matches_forbidden_prefix(module_name, forbidden_prefixes):
                    kind = "import" if isinstance(node, ast.Import) else "from"
                    violations.append(f"{file}:{node.lineno}: {kind} {module_name}")
    return violations


def _is_docstring_expr(node: ast.AST) -> bool:
    """node가 '단독 문자열 Expr 문장'(docstring 위치)인지. 모듈/클래스/함수의 진짜 첫 문장
    여부는 따지지 않는다 — 브리프의 단순화 규칙("Expr 단독 문장이면 docstring 위치로 허용")을
    그대로 따른다. 주석은 ast에 아예 나타나지 않으므로 이 함수와 무관하게 이미 제외된다."""
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _code_value_string_constants(tree: ast.AST) -> list[ast.Constant]:
    """tree 안의 문자열 Constant 노드 중 docstring 위치가 아닌 것만(코드 값으로 쓰인 것)."""
    docstring_ids = {id(node.value) for node in ast.walk(tree) if _is_docstring_expr(node)}
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]


def find_path_literal_violations(
    files: list[Path],
    markers: tuple[str, ...],
    exemptions: dict[Path, tuple[str, ...]] | None = None,
) -> list[str]:
    """files 각각에서 docstring이 아닌 문자열 상수 중 markers를 포함하는 값을 찾는다.

    Args:
        files: 검사 대상 .py 경로 목록.
        markers: 금지 경로 서브스트링(예: "data/scenarios", "ground_truth").
        exemptions: 파일별로 예외 허용할 marker 목록(예: generate_dataset.py는 설정 로딩
            목적으로 "data/scenarios"만 허용). 지정하지 않은 marker는 그 파일에서도 계속
            위반으로 취급한다.

    Returns:
        위반 설명 문자열 리스트("path:lineno: contains 'marker'"). 비어 있으면 위반 없음.
    """
    exemptions = exemptions or {}
    violations: list[str] = []
    for file in files:
        tree = ast.parse(file.read_text(encoding="utf-8"), filename=str(file))
        exempt_markers = exemptions.get(file, ())
        for node in _code_value_string_constants(tree):
            for marker in markers:
                if marker in exempt_markers:
                    continue
                if marker in node.value:
                    violations.append(f"{file}:{node.lineno}: contains {marker!r}")
    return violations


def _python_files(directory: Path) -> list[Path]:
    """directory 아래 모든 .py를 정렬된 리스트로 모은다. 검사 함수가 소비할 '파일 목록'을
    만드는 유일한 디렉터리 순회 지점 — 검사 함수 자체는 항상 리스트를 인자로만 받는다."""
    return sorted(directory.rglob("*.py"))


def _schema_columns() -> list[tuple[str, str]]:
    """schema.sql을 :memory: DB에 적용해 (table, column) 전체를 PRAGMA로 조회한다."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return [
            (table, col[1])
            for table in tables
            for col in conn.execute(f"PRAGMA table_info({table})")
        ]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 허용 목록(각 항목에 사유 명시 — 브리프 §3)
# ---------------------------------------------------------------------------

MEDSUPPLY_FILES = _python_files(REPO_ROOT / "medsupply")
APP_PY_FILE = REPO_ROOT / "app.py"

#: 순방향 검사(임포트 금지 + 경로 리터럴 금지) 대상 — 브리프 §1: medsupply/ 전체 + app.py.
#: scripts/measure_detection.py·eval/ 이하는 정답 접근이 설계상 허용된 경로라 전면 제외한다
#: (eval/에는 현재 .py가 없어 애초에 glob 대상에도 포함되지 않는다).
FORWARD_TARGETS = [*MEDSUPPLY_FILES, APP_PY_FILE]

#: 순방향 "경로 리터럴" 검사에만 추가되는 scripts/ 개별 파일과, 파일별 data/scenarios 예외
#: (§3: "ground_truth 경로 미사용"은 전부 대상, "data/scenarios"는 설정 로딩/산출물 경로
#: 참조 목적인 generate_dataset·validate_dataset만 예외). scripts.datagen 계열 임포트 금지
#: 검사(FORWARD_TARGETS)에는 포함하지 않는다 — 이 파일들은 scripts.datagen을 재사용하는
#: 애플리케이션/CLI 계층이 정당한 설계이기 때문이다(각 스크립트 자체 docstring이 근거).
SCRIPTS_PATH_TARGETS: dict[Path, tuple[str, ...]] = {
    # 위험 평가 배치 실행기 — medsupply를 직접 import하는 애플리케이션 계층이지만, 정답
    # 경로(data/scenarios·ground_truth)는 이 파일에서도 전면 금지(파일 자체 docstring 명시).
    REPO_ROOT / "scripts" / "run_risk_batch.py": (),
    # 공고 원문 부트스트랩 로더 — medsupply 미import, 정답 경로도 전면 금지.
    REPO_ROOT / "scripts" / "load_notices.py": (),
    # 시나리오 config 기본 경로(--config data/scenarios/scenario_config.yaml) 로딩 목적으로만
    # data/scenarios 허용. ground_truth는 라벨을 산출(쓰기)만 하고 읽지 않으므로 계속 금지.
    REPO_ROOT / "scripts" / "generate_dataset.py": ("data/scenarios",),
    # --expect-hash의 산출물 경로 안내(@data/scenarios/standard_snapshot.sha256) 목적으로만
    # data/scenarios 허용. ground_truth 라벨 자체는 읽지 않으므로 계속 금지.
    REPO_ROOT / "scripts" / "validate_dataset.py": ("data/scenarios",),
    # 공고 추출·매핑 처리기 — medsupply를 import하는 애플리케이션 계층이지만, 정답 경로는
    # 전면 금지.
    REPO_ROOT / "scripts" / "process_notices.py": (),
    # 수요예측 MAPE 백테스트 CLI(Task S-19) — stock_usage_daily 실측 사용량만 대조하며
    # ground truth 라벨·시나리오 설정은 전혀 참조하지 않는다(브리프: "ground truth 라벨은
    # 전혀 읽지 않는다" — 실사용량 대조 백테스트라 라벨 접근이 애초에 불필요). data/scenarios
    # 예외도 두지 않는다(generate_dataset.py·validate_dataset.py와 달리 시나리오 설정을
    # 로딩할 이유가 없다).
    REPO_ROOT / "scripts" / "measure_mape.py": (),
}

FORWARD_PATH_TARGETS = [*FORWARD_TARGETS, *SCRIPTS_PATH_TARGETS]


# ---------------------------------------------------------------------------
# 저장소 전역 검사(순방향 임포트 / 순방향 경로 ×2 / 역방향 / schema)
# ---------------------------------------------------------------------------


def test_medsupply_and_app_do_not_import_scenario_answer_modules() -> None:
    """순방향(로직→정답 차단): medsupply/ 전체 + app.py는 scripts.datagen·
    scripts.measure_detection 계열을 임포트하지 않는다."""
    violations = find_forbidden_imports(FORWARD_TARGETS, FORBIDDEN_IMPORT_PREFIXES)
    assert violations == [], "정답 생성/측정 모듈을 임포트하는 로직 코드 발견:\n" + "\n".join(
        violations
    )


def test_forward_scope_does_not_reference_ground_truth_path() -> None:
    """순방향 경로 리터럴: medsupply/ + app.py + scripts 지정 파일 전부에서 ground_truth를
    코드 값으로 쓰지 않는다(예외 없음 — measure_detection.py만 전면 제외된 별도 경로)."""
    violations = find_path_literal_violations(FORWARD_PATH_TARGETS, ("ground_truth",))
    assert violations == [], "ground_truth 경로 리터럴을 코드 값으로 쓰는 지점 발견:\n" + "\n".join(
        violations
    )


def test_forward_scope_restricts_data_scenarios_path_to_allowlist() -> None:
    """data/scenarios 경로 리터럴은 generate_dataset.py·validate_dataset.py(설정 로딩/산출물
    경로 참조 목적)에서만 허용되고, 나머지 순방향 대상 파일에서는 금지된다."""
    violations = find_path_literal_violations(
        FORWARD_PATH_TARGETS, ("data/scenarios",), exemptions=SCRIPTS_PATH_TARGETS
    )
    assert violations == [], "허용되지 않은 data/scenarios 경로 리터럴 발견:\n" + "\n".join(
        violations
    )


def test_datagen_does_not_import_medsupply() -> None:
    """역방향(생성기→로직 차단): scripts/datagen/ 전체가 medsupply를 임포트하지 않는다."""
    datagen_files = _python_files(REPO_ROOT / "scripts" / "datagen")
    violations = find_forbidden_imports(datagen_files, ("medsupply",))
    assert violations == [], "medsupply를 임포트하는 데이터 생성기 코드 발견:\n" + "\n".join(
        violations
    )


def test_schema_has_no_scenario_column() -> None:
    """DB 뒷문 차단(결정 20): schema.sql의 어떤 컬럼명도 'scenario'를 포함하지 않는다.

    scripts/validate_dataset.py의 check_no_scenario_columns가 이미 런타임에 같은 검사를
    하지만, 스키마 자체를 정적으로도 고정해 스키마 변경 시 이 테스트가 먼저 걸리게 한다.
    """
    offenders = [f"{table}.{col}" for table, col in _schema_columns() if "scenario" in col.lower()]
    assert offenders == [], f"'scenario' 포함 컬럼 발견(격리 뒷문): {offenders}"


# ---------------------------------------------------------------------------
# 자가 검증 — 검사 함수가 위반 샘플을 실제로 잡아내는지(그리고 docstring은 허용하는지) 확인.
# ---------------------------------------------------------------------------


def test_self_check_catches_forbidden_import(tmp_path: Path) -> None:
    """find_forbidden_imports가 scripts.datagen 계열 임포트 위반 샘플 1건을 잡아내는지 검증."""
    sample = tmp_path / "violating_module.py"
    sample.write_text(
        "from scripts.datagen.baseline import generate_baseline\n", encoding="utf-8"
    )
    violations = find_forbidden_imports([sample], FORBIDDEN_IMPORT_PREFIXES)
    assert len(violations) == 1
    assert "violating_module.py" in violations[0]


def test_self_check_catches_path_literal_violation(tmp_path: Path) -> None:
    """find_path_literal_violations가 코드 값(모듈 상수)에 쓰인 ground_truth 경로 위반
    1건을 잡아내는지 검증."""
    sample = tmp_path / "violating_path.py"
    sample.write_text(
        'LABELS_PATH = "data/scenarios/ground_truth/standard_v1.json"\n', encoding="utf-8"
    )
    violations = find_path_literal_violations([sample], PATH_LITERAL_MARKERS)
    assert len(violations) >= 1


def test_self_check_allows_docstring_path_mention(tmp_path: Path) -> None:
    """docstring 안의 경로 서술(medsupply/settings.py:4와 동일한 패턴)은 위반으로 잡히지
    않아야 한다."""
    sample = tmp_path / "documented_module.py"
    sample.write_text(
        '"""격리 원칙: data/scenarios/ 이하 경로와 ground_truth는 참조하지 않는다."""\n'
        "\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    violations = find_path_literal_violations([sample], PATH_LITERAL_MARKERS)
    assert violations == []
