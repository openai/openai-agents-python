from agents._mcp_tool_metadata import (
    collect_mcp_list_tools_metadata,
    resolve_mcp_tool_description_for_model,
    resolve_mcp_tool_title,
)


def test_mcp_tool_title_skips_whitespace_explicit_title() -> None:
    tool = {"title": "   ", "annotations": {"title": "Annotated"}}
    assert resolve_mcp_tool_title(tool) == "Annotated"


def test_mcp_model_description_skips_whitespace_description() -> None:
    tool = {"description": "\t ", "title": "Short"}
    assert resolve_mcp_tool_description_for_model(tool) == "Short"


def test_mcp_metadata_collection_skips_whitespace_identifiers() -> None:
    items = [
        {
            "type": "mcp_list_tools",
            "server_label": "github",
            "tools": [
                {"name": "   ", "description": "ignored"},
                {"name": "search", "description": "kept"},
            ],
        }
    ]

    assert list(collect_mcp_list_tools_metadata(items)) == [("github", "search")]
