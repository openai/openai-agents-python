"""The ordinary preview runtime and its native MCP declarations have no Tasks dependency."""

import subprocess
import sys


def test_default_import_avoids_optional_mcp_client() -> None:
    program = """
import sys
class WithoutOptionalMCP:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in {'mcp', 'mcp_types', 'httpx2', 'jsonschema'}:
            raise AssertionError('The default runtime tried to import an optional MCP dependency')
sys.meta_path.insert(0, WithoutOptionalMCP())
from agents import Agent, FunctionTool, HttpMCP, StdioMCP
from agents.experimental.deferred import DeferredTools
assert 'agents.experimental.mcp_tasks' not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, timeout=15)
