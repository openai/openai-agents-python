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
