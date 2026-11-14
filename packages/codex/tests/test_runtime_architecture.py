"""Keep public imports stable while implementation dependencies stay private."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import pickle
import subprocess
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import version
from pathlib import Path
from types import ModuleType
from typing import Literal, get_origin, get_type_hints

import pytest
from typing_extensions import ReadOnly

import _agents_codex
import agents

_RUNTIME = Path(_agents_codex.__file__).parent
_PACKAGE = Path(agents.__file__).parent
_LEAVES = {"config", "version"}
# Only these compatibility modules forward to the retained Classic implementation.
# Other public leaves keep the native-only, explicit-import facade contract.
_CLASSIC_VOICE_MODULES = {
    "realtime.agent",
    "realtime.audio_formats",
    "realtime.config",
    "realtime.events",
    "realtime.handoffs",
    "realtime.items",
    "realtime.model",
    "realtime.model_events",
    "realtime.model_inputs",
    "realtime.openai_realtime",
    "realtime.runner",
    "realtime.session",
    "realtime.testing",
    "voice.events",
    "voice.exceptions",
    "voice.imports",
    "voice.input",
    "voice.model",
    "voice.models.openai_model_provider",
    "voice.models.openai_stt",
    "voice.models.openai_tts",
    "voice.pipeline",
    "voice.pipeline_config",
    "voice.result",
    "voice.testing",
    "voice.utils",
}
_CLASSIC_WORKFLOW_NAMES = {
    "SingleAgentVoiceWorkflow",
    "SingleAgentWorkflowCallbacks",
    "VoiceWorkflowBase",
    "VoiceWorkflowHelper",
}
_PUBLIC_MODULES = sorted(
    {
        ".".join(path.relative_to(_PACKAGE).with_suffix("").parts)
        for path in _PACKAGE.rglob("*.py")
        if path.relative_to(_PACKAGE).parts[0] not in {"classic", "migration"}
        and "_runtime" not in path.relative_to(_PACKAGE).parts
        and path.name != "__init__.py"
        and path.stem not in _LEAVES
    }
    | _CLASSIC_VOICE_MODULES
)


# These Pydantic bases have generated names that cannot appear in import statements.
_GENERIC_BASE_ALIASES = {
    ast.dump(ast.parse(f"globals()[{name}.__bases__[0].__name__] = {name}.__bases__[0]").body[0])
    for name in ("GetOrCancel", "Update")
}


def _require_mcp_tasks() -> None:
    pytest.importorskip("mcp")
    pytest.importorskip("mcp_types")
    if version("mcp") != "2.0.0" or version("mcp-types") != "2.0.0":
        pytest.skip("Requires the qualified MCP 2.0.0 packages")


@pytest.mark.parametrize("module", _PUBLIC_MODULES)
def test_public_modules_only_forward_to_the_runtime(module: str) -> None:
    path = _PACKAGE.joinpath(*module.split(".")).with_suffix(".py")
    tree = ast.parse(path.read_text())
    forwarded = False
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            assert isinstance(statement.value.value, str), path
        elif isinstance(statement, ast.ImportFrom):
            target = (
                importlib.util.resolve_name(
                    "." * statement.level + (statement.module or ""),
                    "agents" + ("." + module.rsplit(".", 1)[0] if "." in module else ""),
                )
                if statement.level
                else statement.module
            )
            if module in _CLASSIC_VOICE_MODULES:
                assert target == "agents.classic." + module, (path, target)
                assert [(alias.name, alias.asname) for alias in statement.names] == [("*", None)]
                forwarded = True
                continue
            assert not any(alias.name == "*" for alias in statement.names), path
            if module == "voice.workflow" and target == "agents.classic.voice.workflow":
                assert {alias.name for alias in statement.names} == _CLASSIC_WORKFLOW_NAMES
                assert all(alias.asname == alias.name for alias in statement.names), path
                continue
            if module == "experimental.mcp_tasks" and target == "agents.experimental":
                assert [(alias.name, alias.asname) for alias in statement.names] == [
                    ("_mcp_tasks_wire", "wire")
                ]
                continue
            assert target == "__future__" or target == "_agents_codex." + module, (
                path,
                target,
            )
            forwarded |= target == "_agents_codex." + module
        elif isinstance(statement, ast.If):
            assert isinstance(statement.test, ast.Name), path
            assert any(
                isinstance(declaration, ast.ImportFrom)
                and declaration.level == 0
                and declaration.module == "_agents_codex." + module
                and any(
                    alias.name == "TYPE_CHECKING" and alias.asname == statement.test.id
                    for alias in declaration.names
                )
                for declaration in tree.body
            ), path
            assert not statement.orelse, path
            for declaration in statement.body:
                assert isinstance(declaration, ast.ImportFrom), path
                assert declaration.level == 0, path
                assert declaration.module == "_agents_codex." + module or (
                    module == "testing" and declaration.module == "_agents_codex.testing_sandbox"
                ), path
                assert all(
                    alias.name != "*" and alias.name == alias.asname for alias in declaration.names
                ), path
        elif isinstance(statement, ast.Assign):
            if (
                module == "experimental._mcp_tasks_wire"
                and ast.dump(statement) in _GENERIC_BASE_ALIASES
            ):
                continue
            assert all(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in statement.targets
            ), path
            assert isinstance(ast.literal_eval(statement.value), (list, tuple)), path
        elif isinstance(statement, ast.AugAssign):
            assert module == "telemetry", path
            assert isinstance(statement.target, ast.Name) and statement.target.id == "__all__", path
            assert isinstance(statement.op, ast.Add), path
            assert isinstance(ast.literal_eval(statement.value), (list, tuple)), path
        else:
            pytest.fail(f"Public facade contains implementation: {path}:{statement.lineno}")
    assert forwarded, path


@pytest.mark.parametrize(
    "module",
    [
        "canonical_storage",
        "canonical_storage_encrypt",
        "canonical_storage_mongodb",
        "canonical_storage_redis",
        "canonical_storage_sqlalchemy",
    ],
)
def test_canonical_adapters_preserve_identity_after_leaf_first_import(module: str) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys
from typing import get_type_hints

importlib.import_module("_agents_codex." + sys.argv[1])
import agents.canonical_storage as public
import _agents_codex.canonical_storage as runtime

for name in (
    "EncryptedSessionStore", "MongoDBSessionStore", "RedisSessionStore", "SQLAlchemySessionStore"
):
    adapter = getattr(public, name)
    assert adapter is getattr(runtime, name), name
    assert adapter.__module__ == "agents.canonical_storage", name
get_type_hints(public.EncryptedSessionStore.__init__)
assert "agents._native" not in sys.modules
""",
            module,
        ],
        check=True,
        timeout=30,
    )


def test_voice_workflow_uses_session_tool_selection_boundary() -> None:
    path = _RUNTIME / "voice" / "workflow.py"
    tree = ast.parse(path.read_text())
    private_session_fields = {
        "_check_open",
        "_effective_agent",
        "_bindings",
        "_executor",
        "_native_agents",
        "_effective_handoffs",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in private_session_fields, (path, node.lineno, node.attr)
        elif isinstance(node, ast.ImportFrom):
            assert all(alias.name != "_split_tools" for alias in node.names), (
                path,
                node.lineno,
            )


def test_runtime_dependencies_do_not_use_public_facades() -> None:
    runtime = _RUNTIME
    assert runtime.is_dir()
    violations = []
    for path in runtime.rglob("*.py"):
        relative = path.relative_to(runtime).with_suffix("")
        parts = relative.parts[:-1]
        package = ".".join(("_agents_codex", *parts))
        for node in ast.walk(ast.parse(path.read_text())):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                target = (
                    importlib.util.resolve_name("." * node.level + (node.module or ""), package)
                    if node.level
                    else node.module
                )
                targets = (
                    [f"{target}.{alias.name}" for alias in node.names]
                    if target == "agents" or node.module is None
                    else [target]
                )
            elif (
                isinstance(node, ast.Call)
                and (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "import_module"
                    or isinstance(node.func, ast.Name)
                    and node.func.id == "import_module"
                )
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                name = node.args[0].value
                targets = [
                    importlib.util.resolve_name(name, package) if name.startswith(".") else name
                ]
            for target in targets:
                # Sandbox bridges share the existing resource and event owners.
                if relative.as_posix() == "sandbox" and target in {
                    "agents.classic.sandbox._mount_security",
                    "agents.classic.sandbox.errors",
                    "agents.classic.sandbox.entries",
                    "agents.classic.sandbox.entries.mounts",
                    "agents.classic.sandbox.manifest",
                    "agents.classic.sandbox.remote_mount_policy",
                    # DockerGateway checks concrete provisioning/lease identities.
                    # Native Core still owns command dispatch and model execution.
                    "agents.classic.sandbox.sandboxes.docker",
                    "agents.classic.sandbox.sandboxes.unix_local",
                    "agents.classic.sandbox.session.sandbox_client",
                    "agents.classic.sandbox.session.sandbox_session",
                    "agents.classic.sandbox.session.events",
                    "agents.classic.sandbox.session.sinks",
                    "agents.classic.sandbox.types",
                    "agents.classic.sandbox.workspace_paths",
                }:
                    continue
                # Profile workspaces use the shared sandbox declarations and local resource owner.
                if relative.as_posix() == "_sandbox_profile_workspace" and target in {
                    "agents.classic.sandbox.entries",
                    "agents.classic.sandbox.manifest",
                    "agents.classic.sandbox.sandboxes.unix_local",
                    "agents.classic.sandbox.session.sandbox_session",
                }:
                    continue
                # Keep event envelopes, sinks and dispatcher identity shared with
                # the resource wrappers; do not admit Classic execution engines.
                if relative.as_posix() == "_sandbox_events" and target in {
                    "agents.classic.sandbox.errors",
                    "agents.classic.sandbox.session.base_sandbox_session",
                    "agents.classic.sandbox.session.events",
                    "agents.classic.sandbox.session.manager",
                    "agents.classic.sandbox.session.sandbox_session",
                    "agents.classic.sandbox.session.sinks",
                }:
                    continue
                # Native memory shares provider storage and paths while its phases run in Core.
                if relative.as_posix() == "sandbox_memory" and target in {
                    "agents.classic.sandbox.errors",
                    "agents.classic.sandbox.memory.storage",
                    "agents.classic.sandbox.session.sandbox_session",
                    "agents.classic.sandbox.workspace_paths",
                }:
                    continue
                if target and (target == "agents" or target.startswith("agents.")):
                    if (target.endswith("._native") and target != "agents._native") or not (
                        target == "_agents_codex"
                        or target.startswith("_agents_codex.")
                        or target in {"agents._native", "agents.config", "agents.version"}
                    ):
                        violations.append(f"{path.relative_to(runtime)}:{node.lineno}: {target}")
    assert not violations, "Runtime imports public facades:\n" + "\n".join(violations)


@pytest.mark.parametrize("module", _PUBLIC_MODULES)
def test_facades_preserve_public_object_identity(module: str) -> None:
    if module in {"experimental._mcp_tasks_wire", "experimental.mcp_tasks"}:
        _require_mcp_tasks()
    public = importlib.import_module("agents." + module)
    runtime = importlib.import_module(
        ("agents.classic." if module in _CLASSIC_VOICE_MODULES else "_agents_codex.") + module
    )
    generated_bases = {}
    if module == "experimental._mcp_tasks_wire":
        for model_name in ("GetOrCancel", "Update"):
            base = getattr(runtime, model_name).__bases__[0]
            generated_bases[base.__name__] = base
    for name, value in vars(public).items():
        if module == "experimental.mcp_tasks" and name == "wire":
            assert value is importlib.import_module("agents.experimental._mcp_tasks_wire")
        elif name in generated_bases:
            assert value is generated_bases[name], (module, name)
        elif not name.startswith("_"):
            owner = (
                importlib.import_module("agents.classic.voice.workflow")
                if module == "voice.workflow" and name in _CLASSIC_WORKFLOW_NAMES
                else runtime
            )
            assert getattr(owner, name) is value, (module, name)


@pytest.mark.parametrize(
    ("module", "name"),
    [
        ("agent", "Agent"),
        ("agent", "ContextT"),
        ("_function_tools", "FunctionTool"),
        ("_function_tools", "function_tool"),
        ("_function_tools", "ContextT_co"),
        ("_trace_context", "TraceContext"),
        ("_native_async", "_NativeCompletionError"),
        ("_native_async", "_await_native_completion"),
        ("_native_async", "_await_native_event"),
        ("config", "ReasoningEffort"),
        ("config", "JsonValue"),
        ("session", "Session"),
        ("telemetry", "TraceOptions"),
        ("telemetry", "trace"),
        ("state", "SessionState"),
        ("prepared_request", "PreparedRequest"),
        ("prepared_request", "PreparedToolPresentation"),
        ("input", "InputText"),
        ("executor", "Environment"),
        ("experimental.deferred", "DeferredTools"),
    ],
)
def test_public_metadata_and_pickle_lookup(module: str, name: str) -> None:
    public = importlib.import_module("agents." + module)
    value = getattr(public, name)
    assert value.__module__ == "agents." + module
    assert pickle.loads(pickle.dumps(value)) is value
    if name in agents.__all__:
        assert getattr(agents, name) is value


def test_generated_mcp_generic_bases_keep_their_public_pickle_paths() -> None:
    _require_mcp_tasks()
    from agents.experimental._mcp_tasks_wire import GetOrCancel, Update

    for value, public_lookup in (
        (
            GetOrCancel.__bases__[0],
            b"cagents.experimental._mcp_tasks_wire\n"
            b"Request[TaskParams, Literal['tasks/get', 'tasks/cancel']]\n.",
        ),
        (
            Update.__bases__[0],
            b"cagents.experimental._mcp_tasks_wire\n"
            b"Request[UpdateParams, Literal['tasks/update']]\n.",
        ),
    ):
        assert pickle.loads(public_lookup) is value
        assert pickle.loads(pickle.dumps(value)) is value


def test_public_models_resolve_annotations_without_rebuilding() -> None:
    from agents.agent import ModelSettings
    from agents.state import SessionState
    from agents.telemetry import TraceLifecycleEvent

    assert get_type_hints(ModelSettings)["reasoning_effort"] is str
    assert get_type_hints(TraceLifecycleEvent, include_extras=True)["sequence"] == ReadOnly[int]
    assert SessionState.__pydantic_complete__
    state = SessionState(
        format_version=2,
        codex_revision="synthetic-revision",
        source_session_id="synthetic-session",
        root_metadata={},
        history=[],
    )
    assert SessionState.model_validate_json(state.model_dump_json()) == state


def test_state_schema_resolves_before_adjacent_public_modules_are_imported() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from agents.state import SessionState

assert 'agents.subagents' not in sys.modules
assert 'agents.turn_receipt' not in sys.modules
assert SessionState.__pydantic_complete__
state = SessionState(
    format_version=2,
    codex_revision='synthetic-revision',
    source_session_id='synthetic-session',
    root_metadata={},
    history=[],
    subagents={'worker': {'description': 'Read the synthetic record.'}},
)
assert SessionState.model_validate_json(state.model_dump_json()) == state

from pydantic import TypeAdapter
from agents.subagents import SubagentConfig
from agents.turn_receipt import TurnReceipt

assert TypeAdapter(SubagentConfig).json_schema()['required'] == ['description']
receipt = TypeAdapter(TurnReceipt).json_schema()
assert 'instruction_files' in receipt['required']
assert receipt['$defs']['InstructionFileReceipt']['required'] == ['path', 'source', 'sha256']
assert 'agents._native' not in sys.modules
""",
        ],
        check=True,
    )


def test_typed_dict_rehoming_preserves_resolved_qualifiers_and_keys(monkeypatch) -> None:
    from types import ModuleType

    from typing_extensions import NotRequired, Required

    from _agents_codex._public import preserve_module_identity

    module = ModuleType("_agents_codex.synthetic_annotations")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    exec(
        """
from __future__ import annotations
from typing_extensions import NotRequired, ReadOnly, Required, TypedDict

class Record(TypedDict, total=False):
    required: Required[int]
    optional: NotRequired[str]
    readonly: ReadOnly[int]
""",
        vars(module),
    )
    record = module.Record
    keys = (record.__required_keys__, record.__optional_keys__, record.__readonly_keys__)
    public_name = "agents.synthetic_annotations"
    assert public_name not in sys.modules

    preserve_module_identity(vars(module), public_name)

    assert module.Record is record
    assert record.__module__ == public_name
    assert (record.__required_keys__, record.__optional_keys__, record.__readonly_keys__) == keys
    assert get_type_hints(record, include_extras=True) == {
        "required": Required[int],
        "optional": NotRequired[str],
        "readonly": ReadOnly[int],
    }
    assert public_name not in sys.modules


def test_public_typed_dict_annotations_preserve_inheritance_and_qualifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing_extensions import NotRequired, Required

    from _agents_codex._public import preserve_module_identity

    implementation = ModuleType("_agents_codex._annotation_test")
    public = ModuleType("agents._annotation_test")
    monkeypatch.setitem(sys.modules, implementation.__name__, implementation)
    monkeypatch.setitem(sys.modules, public.__name__, public)
    exec(
        """
from __future__ import annotations
from typing_extensions import NotRequired, ReadOnly, Required, TypedDict

class Parent(TypedDict, total=False):
    required: Required[int]
    readonly: ReadOnly[str]

class Child(Parent):
    next: NotRequired[Child]
""",
        vars(implementation),
    )
    preserve_module_identity(vars(implementation), public.__name__)
    parent, child = implementation.Parent, implementation.Child
    public.Parent, public.Child = parent, child
    assert parent.__module__ == child.__module__ == public.__name__
    assert pickle.loads(pickle.dumps(child)) is child
    assert get_type_hints(child, include_extras=True) == {
        "required": Required[int],
        "readonly": ReadOnly[str],
        "next": NotRequired[child],
    }


def test_public_dataclass_values_round_trip_through_pickle() -> None:
    from agents.rate_limits import ApiRateLimits, ApiRateLimitWindow
    from agents.telemetry import TraceOptions

    for value in (ApiRateLimits(requests=ApiRateLimitWindow(limit=8)), TraceOptions(disabled=True)):
        restored = pickle.loads(pickle.dumps(value))
        assert restored == value
        assert type(restored) is type(value)


def test_inherited_event_fields_remain_available_to_schema_readers() -> None:
    from pydantic import TypeAdapter, ValidationError

    from agents._events import CommandOutputDeltaEvent

    class ApplicationEvent(CommandOutputDeltaEvent):
        label: str

    annotations = get_type_hints(ApplicationEvent, include_extras=True)
    assert annotations["event_id"] is str
    assert annotations["session_id"] is str
    assert annotations["turn_id"] == str | None
    payload = {
        "event_id": "synthetic-event",
        "session_id": "synthetic-session",
        "turn_id": None,
        "type": "agent.output.command_execution_output.delta",
        "item_id": "synthetic-item",
        "output_index": 0,
        "delta": "synthetic-output",
        "label": "application",
    }
    adapter = TypeAdapter(ApplicationEvent)
    assert adapter.validate_python(payload) == payload
    with pytest.raises(ValidationError, match="event_id"):
        adapter.validate_python({key: value for key, value in payload.items() if key != "event_id"})


def test_annotations_resolve_after_public_module_metadata_is_restored() -> None:
    from agents import FileSearch, FunctionTool, Session, Turn
    from agents.input import InputText
    from agents.observation import HostedToolCallProgress
    from agents.rate_limits import ApiRateLimits, ApiRateLimitWindow
    from agents.telemetry import SpanContext, TraceOptions, TraceScope, trace
    from agents.usage import RunBudget, TokenUsage

    assert get_type_hints(trace)["return"] is TraceScope
    assert get_type_hints(FunctionTool)["name"] is str
    assert get_type_hints(FileSearch)["vector_store_ids"] == Sequence[str]
    assert get_origin(get_type_hints(HostedToolCallProgress)["event"]) is dict
    assert get_type_hints(InputText)["text"] is str
    assert get_type_hints(TraceOptions)["parent"] == SpanContext | None
    assert get_type_hints(ApiRateLimits)["requests"] == ApiRateLimitWindow | None
    assert get_origin(get_type_hints(Session.submit)["return"]) is Turn
    budget_hints = get_type_hints(RunBudget)
    assert budget_hints["estimate_cost_micros"] == Callable[[TokenUsage], int | None] | None
    assert budget_hints["missing_usage"] == Literal["stop", "continue"]


def test_import_and_discovery_leave_native_binding_unloaded() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import sys
import agents
assert '_agents_codex' not in sys.modules
assert 'agents._native' not in sys.modules
before = set(sys.modules)
assert set(agents.__all__) <= set(dir(agents))
assert set(sys.modules) == before
for module in ('agent', 'session', 'telemetry', 'experimental.deferred', 'testing'):
    importlib.import_module('agents.' + module)
    assert 'agents._native' not in sys.modules, module
    if module == 'session':
        assert not any(
            name.startswith(('agents.migration', 'agents.classic')) for name in sys.modules
        )
assert agents.Agent is importlib.import_module('agents.agent').Agent
""",
        ],
        check=True,
    )


def test_runtime_imports_initialize_without_public_facades() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import _agents_codex.session
assert 'agents._native' not in sys.modules
public = {
    name for name in sys.modules
    if name == 'agents' or name.startswith('agents.')
    if not (name == '_agents_codex' or name.startswith('_agents_codex.'))
}
assert public <= {'agents', 'agents.config', 'agents.version'}, sorted(public)
""",
        ],
        check=True,
    )


@pytest.mark.skipif(sys.version_info < (3, 12), reason="stdlib TypeAliasType requires Python 3.12")
def test_immutable_type_aliases_preserve_public_contracts_in_a_fresh_process() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib
import pickle
import sys
import typing
import typing_extensions

assert 'agents' not in sys.modules
# Use the actual immutable constructor even when typing_extensions provides a mutable backport.
typing_extensions.TypeAliasType = typing.TypeAliasType

import agents
assert '_agents_codex' not in sys.modules
from agents import Agent, Session
assert Agent is agents.Agent
assert Session is agents.Session

aliases = {
    '_events': (
        'ElicitationRequest', 'RequiredAction', 'ApprovalDecision', 'EnvironmentStatus',
        'OutputItemStatus', 'FunctionCallStatus', 'WebSearchAction', 'ExecutionItem',
        'OutputItem', 'SessionItem', 'SessionEvent',
    ),
    'subagents': ('_Nonblank', '_Nickname'),
}
for module, names in aliases.items():
    public = importlib.import_module('agents.' + module)
    runtime = importlib.import_module('_agents_codex.' + module)
    for name in names:
        alias = getattr(public, name)
        assert type(alias) is typing.TypeAliasType, (module, name)
        assert alias is getattr(runtime, name), (module, name)
        assert alias.__module__ == 'agents.' + module, (module, name)
        assert pickle.loads(pickle.dumps(alias)) is alias, (module, name)

from pydantic import TypeAdapter, ValidationError
from agents._events import ElicitationRequest
from agents.subagents import _Nickname

form = {
    'mode': 'form',
    'message': 'Choose a synthetic option.',
    'requested_schema': {'type': 'object', 'properties': {}},
}
assert TypeAdapter(ElicitationRequest).validate_python(form) == form
nickname = TypeAdapter(_Nickname)
assert nickname.validate_python('  Reviewer_1  ') == 'Reviewer_1'
for adapter, invalid in (
    (TypeAdapter(ElicitationRequest), {'mode': 'form', 'message': 'Missing schema.'}),
    (nickname, 'invalid!'),
):
    try:
        adapter.validate_python(invalid)
    except ValidationError:
        pass
    else:
        raise AssertionError('Invalid alias value was accepted: ' + repr(invalid))
assert 'agents._native' not in sys.modules
""",
        ],
        check=True,
        timeout=30,
    )


def test_fresh_public_checkpoint_preserves_required_annotations() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json
import sys
from pydantic import ValidationError
from agents.state import SessionState
assert SessionState.__pydantic_complete__
payload = {
    'format_version': 2, 'codex_revision': 'synthetic',
    'source_session_id': 'synthetic', 'history': [], 'root_metadata': {},
    'subagents': {'worker': {'description': 'Synthetic role'}},
}
state = SessionState.from_json(json.dumps(payload))
assert SessionState.from_json(state.to_json()) == state
try:
    SessionState.from_json(json.dumps({**payload, 'subagents': {'worker': {}}}))
except ValidationError:
    pass
else:
    raise AssertionError('Required role description was lost')

assert 'agents._native' not in sys.modules
""",
        ],
        check=True,
        timeout=30,
    )


@pytest.mark.skipif(
    sys.version_info < (3, 14), reason="Deferred TypedDict annotations require 3.14"
)
def test_public_metadata_keeps_unresolved_annotation_bindings() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import annotationlib
import pickle
import sys
import types
from typing import get_args, get_type_hints
from typing_extensions import ReadOnly, Required
from _agents_codex._public import preserve_module_identity

private = types.ModuleType('_agents_codex._annotation_probe')
public = types.ModuleType('agents._annotation_probe')
sys.modules[private.__name__] = private
sys.modules[public.__name__] = public
exec(chr(10).join([
    'from __future__ import annotations',
    'from typing_extensions import TypedDict, Required, ReadOnly',
    'class Deferred(TypedDict):',
    '    value: Required[ReadOnly[Later]]',
]), private.__dict__)
preserve_module_identity(private.__dict__, public.__name__)
public.Deferred = private.Deferred
assert private.Deferred.__module__ == public.__name__
annotation = private.Deferred.__annotations__['value']
assert isinstance(get_args(get_args(annotation)[0])[0], annotationlib.ForwardRef)
private.Later = int
hints = get_type_hints(private.Deferred, globalns=private.__dict__, include_extras=True)
assert hints['value'] == Required[ReadOnly[int]]
assert pickle.loads(pickle.dumps(private.Deferred)) is private.Deferred
""",
        ],
        check=True,
        timeout=30,
    )


def test_testing_model_imports_do_not_require_classic_dependencies() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import importlib.abc
import sys
class NoClassic(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'agents.classic' or fullname.split('.')[0] in {
            'openai', 'griffe', 'requests', 'websockets', 'mcp', 'certifi',
            'httpx2', 'jwt', 'multipart', 'starlette', 'urllib3'
        }:
            raise ModuleNotFoundError('Classic dependency unavailable', name=fullname)
sys.meta_path.insert(0, NoClassic())
from agents.testing import ScriptedModel, ScriptedResponses
assert ScriptedModel is not None and ScriptedResponses is not None
assert not any(name.startswith('agents.classic') for name in sys.modules)
assert '_agents_codex.testing_sandbox' not in sys.modules
""",
        ],
        check=True,
    )


def test_scripted_sandbox_import_is_safe_in_a_fresh_process() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from agents.testing import scripted_sandbox_session
from agents.sandbox import SandboxSession
resource = scripted_sandbox_session()
assert isinstance(resource, SandboxSession)
assert resource.calls == ()
resource.assert_complete()
""",
        ],
        check=True,
    )


# These established leaves have no authority to acquire a Session or native handle.
# _events is an existing shared event-contract port, not a native-free closure.
_DOMAIN_DEPENDENCIES = {
    "_stored_history": (set(), set()),
    "_mcp_names": ({"_agents_codex._public"}, set()),
    "_public": (set(), set()),
    "_web_projection": (
        {"_agents_codex._events", "_agents_codex._public", "_agents_codex._tool_response_types"},
        {"_agents_codex._events", "_agents_codex._tool_response_types"},
    ),
}


def _runtime_dependency_edges(
    source: str, module: str, *, package_module: bool = False
) -> list[tuple[str | None, int]]:
    package = module if package_module else module.rpartition(".")[0]
    edges: list[tuple[str | None, int]] = []
    importlibs, import_modules, typing_modules, type_guards = (
        {"importlib"},
        {"__import__"},
        {"typing"},
        set(),
    )

    builtin_imports = {"__import__"}

    def resolve(name: str, owner: str = package) -> str:
        return importlib.util.resolve_name(name, owner) if name.startswith(".") else name

    def guard(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Name)
            and node.id in type_guards
            or (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in typing_modules
                and node.attr == "TYPE_CHECKING"
            )
        )

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and guard(node.test):
            for child in node.orelse:
                visit(child)
            return
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp)
            and (isinstance(node.test.op, ast.Not) and guard(node.test.operand))
        ):
            for child in node.body:
                visit(child)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                edges.append((alias.name, node.lineno))
                if alias.name == "importlib":
                    importlibs.add(alias.asname or "importlib")
                if alias.name == "typing":
                    typing_modules.add(alias.asname or "typing")
        elif isinstance(node, ast.ImportFrom):
            target = resolve("." * node.level + (node.module or ""))
            if target == "importlib":
                import_modules.update(
                    a.asname or a.name for a in node.names if a.name == "import_module"
                )
            if target == "typing":
                type_guards.update(
                    a.asname or a.name for a in node.names if a.name == "TYPE_CHECKING"
                )
            if node.module is None or target in {"agents", "_agents_codex"}:
                edges.extend((target + "." + a.name, node.lineno) for a in node.names)
            else:
                edges.append((target, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            names = {
                target.id
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                if isinstance(target, ast.Name)
            }
            value = node.value
            if isinstance(value, ast.Name) and value.id in importlibs:
                importlibs.update(names)
            if isinstance(value, ast.Name) and value.id in typing_modules:
                typing_modules.update(names)
            if isinstance(value, ast.Name) and value.id in builtin_imports:
                builtin_imports.update(names)
            if (isinstance(value, ast.Name) and value.id in import_modules) or (
                isinstance(value, ast.Attribute)
                and isinstance(value.value, ast.Name)
                and value.value.id in importlibs
                and value.attr == "import_module"
            ):
                import_modules.update(names)
        elif isinstance(node, ast.Call):
            dynamic = (
                isinstance(node.func, ast.Name) and node.func.id in import_modules | {"__import__"}
            ) or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlibs
                and node.func.attr == "import_module"
            )
            if dynamic:
                literal = node.args[0] if node.args else None
                if isinstance(node.func, ast.Name) and node.func.id in builtin_imports:
                    level = (
                        node.args[4]
                        if len(node.args) > 4
                        else next((kw.value for kw in node.keywords if kw.arg == "level"), None)
                    )
                    # Builtin globals/locals/fromlist are not import_module's package.
                    # Relative builtin imports need globals-based resolution, outside this guard.
                    absolute = level is None or (
                        isinstance(level, ast.Constant) and level.value == 0
                    )
                    edges.append(
                        (
                            literal.value
                            if absolute
                            and isinstance(literal, ast.Constant)
                            and isinstance(literal.value, str)
                            and not literal.value.startswith(".")
                            else None,
                            node.lineno,
                        )
                    )
                    for child in ast.iter_child_nodes(node):
                        visit(child)
                    return
                owner = package
                supplied_owner = (
                    node.args[1]
                    if len(node.args) > 1
                    else next((kw.value for kw in node.keywords if kw.arg == "package"), None)
                )
                if isinstance(supplied_owner, ast.Constant) and isinstance(
                    supplied_owner.value, str
                ):
                    owner = supplied_owner.value
                elif supplied_owner is not None and not (
                    isinstance(supplied_owner, ast.Name) and supplied_owner.id == "__package__"
                ):
                    edges.append((None, node.lineno))
                    return
                edges.append(
                    (
                        resolve(literal.value, owner)
                        if isinstance(literal, ast.Constant) and isinstance(literal.value, str)
                        else None,
                        node.lineno,
                    )
                )
        for child in ast.iter_child_nodes(node):
            visit(child)

    tree = ast.parse(source)
    # Function bodies can use module import aliases declared later. Discover
    # those aliases before emitting edges; retain the same erased guards.
    while True:
        previous = (
            len(importlibs),
            len(import_modules),
            len(typing_modules),
            len(type_guards),
            len(builtin_imports),
        )
        edges.clear()
        visit(tree)
        if previous == (
            len(importlibs),
            len(import_modules),
            len(typing_modules),
            len(type_guards),
            len(builtin_imports),
        ):
            return edges


def _domain_dependency_violations(sources: dict[str, str], root: str) -> list[str]:
    local, boundaries = _DOMAIN_DEPENDENCIES[root]
    # annotationlib is stdlib on supported Python 3.14, but absent from older hosts.
    external = sys.stdlib_module_names | {"annotationlib", "typing_extensions"}
    violations = []
    visited = set()

    def walk(module: str, chain: list[str]) -> None:
        if module in visited:
            return
        visited.add(module)
        if module not in sources:
            violations.append(" -> ".join(chain) + ": missing private source")
            return
        for target, line in _runtime_dependency_edges(sources[module], module):
            route = " -> ".join([*chain, target or "<computed import>"])
            if target is None:
                violations.append(f"{route} at {module}:{line}: unresolved import")
            elif target == "agents" or target.startswith(("agents.", "_agents_")):
                if target not in local:
                    violations.append(f"{route} at {module}:{line}: unreviewed private/facade edge")
                elif target not in boundaries:
                    walk(target, [*chain, target])
            elif target.split(".")[0] not in external:
                violations.append(f"{route} at {module}:{line}: unreviewed external dependency")

    module = "_agents_codex." + root
    walk(module, [module])
    return violations


@pytest.mark.parametrize("root", _DOMAIN_DEPENDENCIES)
def test_domain_runtime_dependency_direction(root: str) -> None:
    sources = {"_agents_codex." + path.stem: path.read_text() for path in _RUNTIME.glob("*.py")}
    violations = _domain_dependency_violations(sources, root)
    assert not violations, "Domain runtime dependencies:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("import _agents_codex.session", {"_agents_codex.session"}),
        ("from .session import Session", {"_agents_codex.session"}),
        ("from . import session", {"_agents_codex.session"}),
        ("from agents import Agent", {"agents.Agent"}),
        (
            "from typing import TYPE_CHECKING as checking\n"
            "if checking:\n    from .session import Session\n"
            "else:\n    import json",
            {"typing", "json"},
        ),
        (
            "import typing as t\nif not t.TYPE_CHECKING:\n    import json\n"
            "else:\n    from .session import Session",
            {"typing", "json"},
        ),
        (
            "def later():\n    return load('.session', __package__)\n"
            "from importlib import import_module as load",
            {"importlib", "_agents_codex.session"},
        ),
        (
            "import importlib as loader\nload = loader.import_module\n"
            "load('.session', '_agents_codex')",
            {"importlib", "_agents_codex.session"},
        ),
        ("__import__('_agents_codex.session')", {"_agents_codex.session"}),
        (
            "__import__('_agents_codex.session', globals(), locals(), ['Session'], 0)",
            {"_agents_codex.session"},
        ),
        ("__import__('session', globals(), locals(), ['Session'], 1)", {None}),
        ("load = __import__\nload('session', level=1)", {None}),
        ("__import__('json', __import__('numpy').__dict__)", {"json", "numpy"}),
        ("__import__(selected_module)", {None}),
    ],
)
def test_domain_dependency_parser_controls(source: str, expected: set[str | None]) -> None:
    assert {
        target for target, _ in _runtime_dependency_edges(source, "_agents_codex.leaf")
    } == expected


@pytest.mark.parametrize(
    "source,rejected",
    [
        ("from .session import Session", "_agents_codex.session"),
        ("from ._opening import _capture_opening_agent", "_agents_codex._opening"),
        ("from agents import Agent", "agents.Agent"),
        ("__import__('_agents_codex.session')", "_agents_codex.session"),
        ("__import__(selected_module)", "<computed import>"),
        ("import numpy", "numpy"),
    ],
)
def test_domain_dependency_negative_controls(source: str, rejected: str) -> None:
    violations = _domain_dependency_violations(
        {"_agents_codex._stored_history": source}, "_stored_history"
    )
    assert any(rejected in violation for violation in violations), violations


def test_domain_dependency_rejects_backedge_through_allowed_helper() -> None:
    sources = {
        "_agents_codex._mcp_names": "from ._public import preserve_module_identity",
        "_agents_codex._public": "from .session import Session",
    }
    violations = _domain_dependency_violations(sources, "_mcp_names")
    assert any(
        "_agents_codex._mcp_names -> _agents_codex._public -> _agents_codex.session" in item
        for item in violations
    )


def test_domain_dependency_requires_named_source() -> None:
    violations = _domain_dependency_violations({}, "_stored_history")
    assert violations == ["_agents_codex._stored_history: missing private source"]


def test_domain_dependency_accepts_standard_library_imports() -> None:
    assert not _domain_dependency_violations(
        {"_agents_codex._stored_history": "import statistics"}, "_stored_history"
    )


def test_sandbox_facade_first_import_has_been_initializing_shared_manifest_ownership() -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
assert 'agents.sandbox' not in sys.modules
import agents.sandbox as sandbox
from _agents_shared.sandbox_manifest import _borrowed_manifest_lock
from types import SimpleNamespace
assert callable(sandbox.sandbox_environment)
owner = SimpleNamespace()
assert _borrowed_manifest_lock(owner) is _borrowed_manifest_lock(owner)
assert 'agents._native' not in sys.modules
""",
        ],
        check=True,
    )
