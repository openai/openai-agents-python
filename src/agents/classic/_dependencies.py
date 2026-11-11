"""Check the optional classic dependency boundary without masking import errors."""

from importlib.util import find_spec as _find_spec

# Check direct optional dependencies before importing the classic runtime. Errors
# inside installed packages must retain their original type and traceback.
_classic_dependencies = (
    "openai",
    "griffe",
    "requests",
    "websockets",
    "mcp",
    "certifi",
    "httpx2",
    "jwt",
    "multipart",
    "starlette",
    "urllib3",
)
_missing_classic_dependencies = [name for name in _classic_dependencies if _find_spec(name) is None]
if _missing_classic_dependencies:
    raise ModuleNotFoundError(
        "agents.classic requires the classic extra. Install the matching SDK with [classic]. "
        "See CLASSIC.md#install-the-classic-extra for wheel and source setup. Missing modules: "
        + ", ".join(_missing_classic_dependencies),
        name=_missing_classic_dependencies[0],
    )
