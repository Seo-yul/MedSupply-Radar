"""프롬프트 레지스트리 로더."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from medsupply.llm.client import RenderedPrompt


@dataclass(frozen=True)
class PromptTemplate:
    """프롬프트 템플릿(시스템/사용자 템플릿 + 메타데이터)."""

    task: str
    version: str
    system: str
    user_template: str

    def render(self, **variables) -> RenderedPrompt:
        """템플릿을 렌더링해 RenderedPrompt 생성.

        Args:
            **variables: user_template.format()에 전달할 변수들.
                system은 변수 없음(있어도 함께 format).
                누락 변수는 KeyError 발생.

        Returns:
            렌더링된 RenderedPrompt 인스턴스.

        Raises:
            KeyError: 필수 변수가 누락된 경우.
        """
        rendered_system = self.system.format(**variables) if "{" in self.system else self.system
        rendered_user = self.user_template.format(**variables)
        return RenderedPrompt(
            system=rendered_system,
            user=rendered_user,
            version=self.version,
        )


def _load_registry() -> dict[str, Any]:
    """registry.yaml 로드.

    Returns:
        파싱된 registry dict.
    """
    registry_path = Path(__file__).parent / "registry.yaml"
    with open(registry_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _parse_prompt_file(path: Path) -> tuple[str, str]:
    """프롬프트 마크다운 파일 파싱.

    형식:
    ```
    <!-- system -->
    (시스템 프롬프트)
    <!-- user -->
    (사용자 프롬프트)
    ```

    Args:
        path: 프롬프트 파일 경로.

    Returns:
        (system, user_template) 튜플.

    Raises:
        ValueError: 필수 구분자가 없는 경우.
    """
    content = path.read_text(encoding="utf-8")

    if "<!-- system -->" not in content:
        raise ValueError(f"프롬프트 파일에 '<!-- system -->' 구분자가 없음: {path}")
    if "<!-- user -->" not in content:
        raise ValueError(f"프롬프트 파일에 '<!-- user -->' 구분자가 없음: {path}")

    # system 구분자부터 user 구분자까지 추출
    system_start = content.find("<!-- system -->") + len("<!-- system -->")
    system_end = content.find("<!-- user -->")
    system = content[system_start:system_end].strip()

    # user 구분자 이후 끝까지 추출
    user_start = content.find("<!-- user -->") + len("<!-- user -->")
    user_template = content[user_start:].strip()

    return system, user_template


def load_prompt(task: str, version: str | None = None) -> PromptTemplate:
    """프롬프트 템플릿 로드.

    Args:
        task: 태스크 이름 (예: "notice_extract", "risk_explain").
        version: 버전 (예: "v1"). None이면 registry의 active 버전 사용.

    Returns:
        PromptTemplate 인스턴스.

    Raises:
        ValueError: 미지 task 또는 version인 경우.
    """
    registry = _load_registry()

    if "tasks" not in registry or task not in registry["tasks"]:
        available_tasks = list(registry.get("tasks", {}).keys())
        raise ValueError(
            f"알 수 없는 task: {task!r}. 사용 가능한 task: {available_tasks}"
        )

    task_info = registry["tasks"][task]

    # version이 None이면 active 버전 사용
    if version is None:
        version = task_info["active"]

    # 버전 존재 확인
    if version not in task_info["versions"]:
        available_versions = list(task_info["versions"].keys())
        raise ValueError(
            f"알 수 없는 version: {version!r} (task={task!r}). "
            f"사용 가능한 version: {available_versions}"
        )

    # 프롬프트 파일 경로
    file_rel = task_info["versions"][version]["file"]
    file_path = Path(__file__).parent / file_rel

    # 파일 파싱
    system, user_template = _parse_prompt_file(file_path)

    return PromptTemplate(
        task=task,
        version=version,
        system=system,
        user_template=user_template,
    )


def list_prompts() -> dict[str, dict]:
    """사용 가능한 프롬프트 요약.

    Returns:
        {"task_name": {"active": "v1", "versions": {"v1": {...}}}} 형태 dict.
    """
    registry = _load_registry()
    result = {}

    for task_name, task_info in registry.get("tasks", {}).items():
        result[task_name] = {
            "active": task_info["active"],
            "versions": task_info["versions"],
        }

    return result
