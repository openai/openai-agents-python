import pytest

from agents import function_tool


def test_programmatic_stays_distinct_from_local_code():
    @function_tool(allowed_callers=["programmatic"])
    def lookup() -> str:
        return "ok"

    assert lookup.allowed_callers == ("programmatic",)

    @function_tool(allowed_callers=["programmatic", "direct"])
    def mixed() -> str:
        return "ok"

    assert mixed.allowed_callers == ("direct", "programmatic")
    assert "code" not in mixed.allowed_callers


def test_default_and_duplicate_caller_policy():
    @function_tool
    def plain() -> str:
        return "ok"

    assert plain.allowed_callers is None
    with pytest.raises(ValueError, match="distinct"):
        function_tool(allowed_callers=["programmatic", "programmatic"])(lambda: "ok")


def test_custom_string_tools_reject_provider_programmatic_callers():
    @function_tool(allowed_callers=["programmatic", "direct"])
    def lookup(query: str) -> str:
        return query

    with pytest.raises(ValueError, match="Custom string tools do not support programmatic callers"):
        lookup.as_custom()

    @function_tool(allowed_callers=["direct", "code"])
    def local_lookup(query: str) -> str:
        return query

    assert local_lookup.as_custom().allowed_callers == ("direct", "code")
