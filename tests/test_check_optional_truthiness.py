from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest


def _load_checker() -> ModuleType:
    path = Path(__file__).parents[1] / ".github/scripts/check_optional_truthiness.py"
    spec = importlib.util.spec_from_file_location("check_optional_truthiness", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


def _violations(tmp_path: Path, source: str) -> list[Any]:
    path = tmp_path / "example.py"
    path.write_text(source, encoding="utf-8")
    return cast(list[Any], checker.find_violations([path]))


@pytest.mark.parametrize(
    ("statement", "expected_expression"),
    [
        ("if callback:\n        callback()", "callback"),
        ("while callback:\n        break", "callback"),
        ("assert callback", "callback"),
        ("return callback if callback else fallback", "callback"),
        ("return not callback", "callback"),
        ("return callback and callback()", "callback"),
        ("return callback or fallback", "callback"),
    ],
)
def test_rejects_supported_boolean_forms(
    tmp_path: Path,
    statement: str,
    expected_expression: str,
) -> None:
    violations = _violations(
        tmp_path,
        f"""
from collections.abc import Callable

def invoke(callback: Callable[[], str] | None, fallback: Callable[[], str]):
    {statement}
""",
    )

    assert [violation.expression for violation in violations] == [expected_expression]


def test_rejects_direct_local_class_parameter_and_local(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
class Model: ...

def select(model: Model | None):
    current: Model | None = model
    if model:
        return model
    return current or Model()
""",
    )

    assert {violation.expression for violation in violations} == {"model", "current"}


def test_rejects_callable_imported_in_function_scope(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
def select(fallback):
    from typing import Callable

    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    )

    assert [violation.expression for violation in violations] == ["callback"]


def test_rejects_callable_imported_in_class_scope_without_leaking(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
class Runner:
    from typing import Callable

    callback: Callable[[], str] | None = None

    def select(self, override: Callable[[], str] | None):
        return self.callback or override or fallback

def unrelated(callback: Callable[[], str] | None):
    return callback or fallback
""",
    )

    assert {violation.expression for violation in violations} == {
        "self.callback",
        "override",
    }


def test_class_scope_callable_shadow_suppresses_only_that_class(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from typing import Callable

class Runner:
    from vendor import Callable

    callback: Callable[[], str] | None = None

    def select(self, override: Callable[[], str] | None):
        return self.callback or override or fallback

def unrelated(callback: Callable[[], str] | None):
    return callback or fallback
""",
    )

    assert [violation.expression for violation in violations] == ["callback"]


@pytest.mark.parametrize(
    "source",
    [
        """
from typing import Callable

def select(callback: Callable[[], str] | None):
    return callback or fallback

Callable = replacement
""",
        """
from typing import Callable

class Runner:
    def select(self, callback: Callable[[], str] | None):
        return callback or fallback

    Callable = replacement
""",
    ],
    ids=["module", "class"],
)
def test_rebound_callable_scope_remains_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


def test_rejects_callbacks_in_nested_class_scopes(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from typing import Callable

def outer():
    class Runner:
        def select(self, callback: Callable[[], str] | None):
            return callback or fallback

class Container:
    class Runner:
        def select(self, callback: Callable[[], str] | None):
            return callback or fallback
""",
    )

    assert [violation.expression for violation in violations] == ["callback", "callback"]


def test_redefined_same_file_class_remains_unclassified(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
class Base: ...
class Model(Base): ...

def select(model: Model | None):
    return model or fallback

class Model: ...
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        """
class Model: ...
from vendor import Model

def select():
    value: Model | None = None
    return value or fallback
""",
        """
class Model: ...

class Runner:
    from vendor import Model
    value: Model | None = None

    def select(self):
        return self.value or fallback
""",
        """
class Model: ...

def select():
    from vendor import Model
    value: Model | None = None
    return value or fallback
""",
        """
class Model: ...

def outer():
    from vendor import Model

    def inner():
        value: Model | None = None
        return value or fallback

    return inner()
""",
    ],
    ids=["module", "class", "function", "nested-function"],
)
def test_same_file_class_shadow_remains_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


@pytest.mark.parametrize("module", [".typing", ".collections.abc"])
def test_relative_callable_import_remains_unclassified(tmp_path: Path, module: str) -> None:
    violations = _violations(
        tmp_path,
        f"""
from {module} import Callable

def select(callback: Callable[[], str] | None):
    return callback or fallback
""",
    )

    assert violations == []


def test_comprehension_binding_does_not_shadow_enclosing_scope(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from typing import Callable

def select(values):
    ignored = [Callable for Callable in values]
    callback: Callable[[], str] | None = None
    return callback or fallback

class Runner:
    ignored = [Callable for Callable in values]
    callback: Callable[[], str] | None = None

    def select(self):
        return self.callback or fallback
""",
    )

    assert {violation.expression for violation in violations} == {
        "callback",
        "self.callback",
    }


def test_comprehension_walrus_shadows_enclosing_scope(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from typing import Callable

def select(values):
    ignored = [value for value in values if (Callable := value)]
    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "definition",
    [
        "def nested(value=(Callable := factory)): ...",
        "factory = lambda value=(Callable := factory): value",
        "@((Callable := decorate))\ndef nested(): ...",
        "class Nested((Callable := Base)): ...",
    ],
    ids=["function-default", "lambda-default", "decorator", "class-base"],
)
def test_definition_expression_walrus_shadows_enclosing_scope(
    tmp_path: Path,
    definition: str,
) -> None:
    indented_definition = definition.replace("\n", "\n    ")
    violations = _violations(
        tmp_path,
        f"""
def outer():
    from typing import Callable

    {indented_definition}
    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "pattern",
    ["Callable", "[*Callable]", "{**Callable}"],
    ids=["match-as", "match-star", "match-mapping-rest"],
)
def test_match_capture_shadows_callable_binding(tmp_path: Path, pattern: str) -> None:
    violations = _violations(
        tmp_path,
        f"""
from typing import Callable

def select(subject):
    match subject:
        case {pattern}:
            pass
    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "binding",
    [
        "Callable = make_type()",
        "def Callable(): ...",
        "from vendor import *",
    ],
    ids=["assignment", "definition", "star-import"],
)
def test_callable_shadow_forms_remain_unclassified(
    tmp_path: Path,
    binding: str,
) -> None:
    violations = _violations(
        tmp_path,
        f"""
from typing import Callable

def select():
    {binding}
    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    )

    assert violations == []


def test_rejects_direct_self_fields_declared_in_class_or_method(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from collections.abc import Callable

class Runner:
    callback: Callable[[], str] | None = None

    def configure(self):
        self.fallback: Callable[[], str] | None = None

    def run(self):
        if self.callback:
            self.callback()
        return self.fallback or default_callback
""",
    )

    assert {violation.expression for violation in violations} == {
        "self.callback",
        "self.fallback",
    }


def test_static_method_parameter_is_checked_without_instance_field_inference(
    tmp_path: Path,
) -> None:
    violations = _violations(
        tmp_path,
        """
from collections.abc import Callable

class Runner:
    callback: Callable[[], str] | None = None

    @staticmethod
    def run(callback: Callable[[], str] | None):
        if callback:
            callback()
""",
    )

    assert [violation.expression for violation in violations] == ["callback"]


def test_static_method_receiver_field_declaration_is_not_inferred(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from collections.abc import Callable

class Runner:
    @staticmethod
    def configure(self):
        self.callback: Callable[[], str] | None = None

    def run(self):
        return self.callback or default_callback
""",
    )

    assert violations == []


def test_qualified_static_method_receiver_field_declaration_is_not_inferred(
    tmp_path: Path,
) -> None:
    violations = _violations(
        tmp_path,
        """
import builtins
from collections.abc import Callable

class Runner:
    @builtins.staticmethod
    def configure(self):
        self.callback: Callable[[], str] | None = None

    def run(self):
        return self.callback or default_callback
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        """
from typing import Callable

def mutate():
    global Callable
    Callable = replacement

def select(callback: Callable[[], str] | None):
    return callback or fallback
""",
        """
def outer():
    from typing import Callable

    def mutate():
        nonlocal Callable
        Callable = replacement

    callback: Callable[[], str] | None = None
    return callback or fallback
""",
    ],
    ids=["global", "nonlocal"],
)
def test_outer_scope_rebinding_remains_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


@pytest.mark.parametrize(
    "source",
    [
        """
from typing import Callable

def inspect():
    global Callable
    return Callable

def select(callback: Callable[[], str] | None):
    return callback or fallback
""",
        """
Callable = replacement

def configure():
    global Callable
    from typing import Callable

def select(callback: Callable[[], str] | None):
    return callback or fallback
""",
        """
def outer():
    from typing import Callable

    callback: Callable[[], str] | None = None

    def middle():
        from typing import Callable

        def inner():
            nonlocal Callable
            Callable = replacement

    return callback or fallback
""",
        """
def outer():
    from typing import Callable

    callback: Callable[[], str] | None = None

    class Mutator:
        nonlocal Callable
        Callable = replacement

    return callback or fallback
""",
        """
def outer():
    from typing import Callable

    callback: Callable[[], str] | None = None

    def middle():
        def inner():
            nonlocal Callable
            Callable = replacement

    return callback or fallback
""",
    ],
    ids=[
        "read-only-global",
        "global-import",
        "deep-nonlocal-with-binding",
        "class-nonlocal",
        "deep-nonlocal-without-binding",
    ],
)
def test_cross_scope_declarations_remain_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 requires Python 3.12+")
@pytest.mark.parametrize(
    "source",
    [
        """
from typing import Callable

def select[Callable](callback: Callable[[], str] | None):
    return callback or fallback
""",
        """
from typing import Callable

class Runner[Callable]:
    callback: Callable[[], str] | None = None

    def select(self):
        return self.callback or fallback
""",
        """
class Model: ...

class Runner[Model]:
    def select(self):
        value: Model | None = None
        return value or fallback
""",
        """
from typing import Callable

class Runner[Callable]:
    def select(self):
        callback: Callable[[], str] | None = None
        return callback or fallback
""",
    ],
    ids=["function", "class-field", "class-method-model", "class-method-callable"],
)
def test_type_parameter_bindings_remain_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


def test_rejects_match_guard_truthiness(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from typing import Callable

def select(value, callback: Callable[[], str] | None):
    match value:
        case _ if callback:
            return callback()
""",
    )

    assert [violation.expression for violation in violations] == ["callback"]


def test_allows_explicit_none_checks_and_direct_value_truthiness(tmp_path: Path) -> None:
    violations = _violations(
        tmp_path,
        """
from collections.abc import Callable

def select(
    callback: Callable[[], str] | None,
    name: str | None,
    count: int | None,
    values: list[str] | None,
    options: dict[str, str] | None,
):
    if callback is not None:
        callback()
    return name or "default", count or 1, values or [], options or {}
""",
    )

    assert violations == []


@pytest.mark.parametrize(
    "source",
    [
        """
from collections.abc import Callable
Callback = Callable[[], str]
def select(callback: Callback | None):
    return callback or fallback
""",
        """
from vendor import ImportedModel
def select(model: ImportedModel | None):
    return model or fallback
""",
        """
import typing
def select(callback: typing.Callable[[], str] | None):
    return callback or fallback
""",
        """
from typing import Annotated, Callable
def select(callback: Annotated[Callable[[], str] | None, "meta"]):
    return callback or fallback
""",
        """
from typing import Callable, Optional
def select(callback: Optional[Callable[[], str]]):
    return callback or fallback
""",
        """
from collections.abc import Callable
class Base:
    callback: Callable[[], str] | None = None
class Child(Base):
    def select(self):
        return self.callback or fallback
""",
        """
from collections.abc import Callable
def select(callback: Callable[[], str] | None):
    current = callback
    return current or fallback
""",
        """
from collections.abc import Callable
class Runner:
    callback: Callable[[], str] | None = None
    def select(self, other: Runner):
        return other.callback or fallback
""",
        """
from collections.abc import Callable
def current() -> Callable[[], str] | None:
    return None
def select():
    return current() or fallback
""",
        """
from collections.abc import Callable
class Runner:
    @property
    def callback(self) -> Callable[[], str] | None:
        return None
    def select(self):
        return self.callback or fallback
""",
        """
from collections.abc import Callable
def select(callback: Callable | None):
    return callback or fallback
""",
        """
class Box: ...
def select(value: Box[int] | None):
    return value or fallback
""",
        """
from typing import Callable as Callback
def select(callback: Callback[[], str] | None):
    return callback or fallback
""",
        """
from collections.abc import Callable
def select(callback: Callable[[], str] | None):
    return bool(callback)
""",
        """
from collections.abc import Callable
def select(callback: Callable[[], str] | None):
    first = True and callback
    second = False or callback
    return first, second
""",
        """
from collections.abc import Callable
def outer(callback: Callable[[], str] | None):
    def inner(value=callback or fallback):
        return value
""",
        """
from collections.abc import Callable
def outer(callback: Callable[[], str] | None):
    @decorate(callback or fallback)
    def inner():
        pass
""",
        """
from collections.abc import Callable
def outer(callback: Callable[[], str] | None):
    return lambda value=callback or fallback: value
""",
        """
from collections.abc import Callable
def outer(callback: Callable[[], str] | None):
    class Inner(callback or fallback):
        pass
""",
        """
from collections.abc import Callable
def outer(callback: Callable[[], str] | None):
    return [value for value in (callback or fallback)]
""",
    ],
    ids=[
        "type-alias",
        "imported-type",
        "qualified-type",
        "annotated",
        "legacy-optional",
        "inherited-field",
        "assignment-flow",
        "nested-receiver",
        "call-return",
        "descriptor-return",
        "bare-callable",
        "parameterized-local-class",
        "aliased-callable-import",
        "bool-call",
        "final-boolean-operand",
        "nested-function-default",
        "nested-function-decorator",
        "lambda-default",
        "nested-class-base",
        "comprehension-first-iterator",
    ],
)
def test_leaves_each_unsupported_category_unclassified(tmp_path: Path, source: str) -> None:
    assert _violations(tmp_path, source) == []


def test_cli_reports_actionable_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "example.py"
    path.write_text(
        """
from collections.abc import Callable

def invoke(callback: Callable[[], str] | None):
    return callback or fallback
""",
        encoding="utf-8",
    )

    result = checker.main([str(path)])
    captured = capsys.readouterr()

    assert result == 1
    assert "optional object uses truthiness: callback" in captured.out
    assert "explicit `is None` or `is not None`" in captured.err
