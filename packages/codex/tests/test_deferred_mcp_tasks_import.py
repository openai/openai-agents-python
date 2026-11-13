"""The ordinary preview runtime and its native MCP declarations have no Tasks dependency."""

import subprocess
import sys


def test_default_import_avoids_optional_mcp_client() -> None:
    program = """
import sys
class WithoutOptionalMCP:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.', 1)[0] in {'mcp', 'mcp_types', 'httpx2', 'jsonschema', 'openai'}:
            raise AssertionError('The default runtime tried to import an optional MCP dependency')
sys.meta_path.insert(0, WithoutOptionalMCP())
from agents import Agent, FunctionTool, HttpMCP, StdioMCP
from agents.experimental.deferred import DeferredTools
assert 'agents.experimental.mcp_tasks' not in sys.modules
agent = Agent(instructions='Answer.', model_settings={
    'extra_headers': {'x-native': 'value', 'x-remove': None},
})
assert dict(agent.model_settings['extra_headers']) == {'x-native': 'value', 'x-remove': None}
assert 'openai' not in sys.modules
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, timeout=15)


def test_header_omissions_use_openai_imported_after_native_declarations() -> None:
    program = """
import sys
from agents import Agent
assert 'openai' not in sys.modules
from openai import Omit, omit
agent = Agent(instructions='Answer.', model_settings={
    'extra_headers': {'x-native': 'value', 'x-none': None, 'x-remove': Omit()},
})
headers = agent.model_settings['extra_headers']
assert headers['x-native'] == 'value'
assert headers['x-none'] is None
assert headers['x-remove'] is omit
"""
    subprocess.run([sys.executable, "-I", "-c", program], check=True, timeout=15)
