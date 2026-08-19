"""프롬프트 로더 및 레지스트리 테스트."""

import pytest
from pathlib import Path

from medsupply.llm.client import RenderedPrompt
from medsupply.llm.prompts.loader import load_prompt, list_prompts, PromptTemplate


class TestLoadPrompt:
    """load_prompt() 기본 동작 테스트."""

    def test_load_notice_extract_default_version(self):
        """load_prompt("notice_extract")는 active v1 로드."""
        template = load_prompt("notice_extract")
        assert template.task == "notice_extract"
        assert template.version == "v1"
        assert template.system  # 비어있지 않음
        assert template.user_template  # 비어있지 않음

    def test_load_risk_explain_default_version(self):
        """load_prompt("risk_explain")는 active v1 로드."""
        template = load_prompt("risk_explain")
        assert template.task == "risk_explain"
        assert template.version == "v1"
        assert template.system
        assert template.user_template

    def test_load_explicit_version(self):
        """버전을 명시하면 해당 버전 로드."""
        template = load_prompt("notice_extract", version="v1")
        assert template.version == "v1"

    def test_unknown_task_raises_valueerror(self):
        """미지 task는 ValueError, 가능한 태스크 나열."""
        with pytest.raises(ValueError) as exc_info:
            load_prompt("unknown_task")
        error_msg = str(exc_info.value).lower()
        assert "notice_extract" in error_msg or "task" in error_msg

    def test_unknown_version_raises_valueerror(self):
        """미지 version은 ValueError."""
        with pytest.raises(ValueError) as exc_info:
            load_prompt("notice_extract", version="v999")
        error_msg = str(exc_info.value).lower()
        assert "version" in error_msg or "v999" in error_msg


class TestPromptTemplateRender:
    """PromptTemplate.render() 테스트."""

    def test_render_basic(self):
        """기본 렌더링: {변수} 치환."""
        template = load_prompt("notice_extract")
        rendered = template.render(raw_text="샘플 공고")

        assert isinstance(rendered, RenderedPrompt)
        assert rendered.system == template.system
        assert "샘플 공고" in rendered.user
        assert rendered.version == "v1"

    def test_render_missing_variable_raises_keyerror(self):
        """누락 변수는 KeyError."""
        template = load_prompt("notice_extract")
        with pytest.raises(KeyError):
            template.render()  # raw_text 필수

    def test_render_risk_explain_multiple_variables(self):
        """risk_explain 렌더링: 여러 변수."""
        template = load_prompt("risk_explain")
        rendered = template.render(
            evidence_json='{"key": "value"}',
            history_json='{"past": "data"}'
        )

        assert isinstance(rendered, RenderedPrompt)
        assert "key" in rendered.user or "value" in rendered.user
        assert rendered.version == "v1"


class TestListPrompts:
    """list_prompts() 테스트."""

    def test_list_prompts_structure(self):
        """list_prompts 구조 검증."""
        prompts = list_prompts()

        assert isinstance(prompts, dict)
        assert "notice_extract" in prompts
        assert "risk_explain" in prompts

        # 각 태스크는 active 버전과 versions dict 포함
        for task_name, task_info in prompts.items():
            assert "active" in task_info
            assert "versions" in task_info
            assert isinstance(task_info["versions"], dict)

    def test_list_prompts_active_version(self):
        """active 버전이 존재."""
        prompts = list_prompts()

        for task_name, task_info in prompts.items():
            active = task_info["active"]
            assert active in task_info["versions"]


class TestPromptFileExistence:
    """레지스트리의 모든 프롬프트 파일 존재 검증."""

    def test_all_registry_files_exist(self):
        """registry에 등재된 모든 파일이 실제로 존재."""
        from medsupply.llm.prompts.loader import _load_registry

        registry = _load_registry()
        prompts_dir = Path(__file__).parent.parent.parent / "medsupply" / "llm" / "prompts"

        for task, task_info in registry["tasks"].items():
            for version, version_info in task_info["versions"].items():
                file_path = prompts_dir / version_info["file"]
                assert file_path.exists(), f"파일이 없음: {file_path}"


class TestParsingErrors:
    """프롬프트 파일 파싱 에러 처리."""

    def test_missing_system_delimiter(self, tmp_path):
        """<!-- system --> 구분자 누락 시 ValueError."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("<!-- user -->\nuser content")

        from medsupply.llm.prompts.loader import _parse_prompt_file

        with pytest.raises(ValueError) as exc_info:
            _parse_prompt_file(bad_file)
        assert "system" in str(exc_info.value).lower()

    def test_missing_user_delimiter(self, tmp_path):
        """<!-- user --> 구분자 누락 시 ValueError."""
        bad_file = tmp_path / "bad.md"
        bad_file.write_text("<!-- system -->\nsystem content")

        from medsupply.llm.prompts.loader import _parse_prompt_file

        with pytest.raises(ValueError) as exc_info:
            _parse_prompt_file(bad_file)
        assert "user" in str(exc_info.value).lower()
