from .tool_output_trimmer import ToolOutputTrimmer

__all__ = ["ToolOutputTrimmer"]


def __getattr__(name: str) -> object:
    """Lazy-load optional extension symbols to avoid import errors for missing extras."""
    if name == "exa_search_tool":
        from .exa_search import exa_search_tool

        return exa_search_tool
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
