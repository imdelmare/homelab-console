from pathlib import Path

from app.tools.governance import APPROVED_WRITE_TOOLS
from app.tools.registry import list_tools


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def test_write_tools_follow_operator_approved_policy():
    tools = list_tools()
    by_id = {tool.id: tool for tool in tools}

    for tool in tools:
        if tool.mode != "write":
            continue
        assert tool.requires_confirmation is True
        if tool.id not in APPROVED_WRITE_TOOLS:
            assert tool.enabled is False

    for tool_id, decision_path in APPROVED_WRITE_TOOLS.items():
        assert tool_id in by_id
        assert by_id[tool_id].mode == "write"
        assert by_id[tool_id].requires_confirmation is True
        path = REPOSITORY_ROOT / decision_path
        assert path.is_file()
        assert path.is_relative_to(REPOSITORY_ROOT / "docs" / "decisions")
