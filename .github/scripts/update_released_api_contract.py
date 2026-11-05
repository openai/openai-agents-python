"""Check the reviewed native API before updating release snapshot metadata."""

from __future__ import annotations

import argparse
import ast
import copy
import importlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, UnionType
from typing import Any, Union, get_origin

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "tests/fixtures/released_api_contract.json"
_PROTOCOL_METHODS = {
    "__init__",
    "__new__",
    "__call__",
    "__enter__",
    "__exit__",
    "__aenter__",
    "__aexit__",
    "__iter__",
    "__next__",
    "__aiter__",
    "__anext__",
}


def _names(node: ast.stmt) -> set[str]:
    if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        return {node.name}
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return {node.target.id}
    if isinstance(node, ast.Assign):
        return {target.id for target in node.targets if isinstance(target, ast.Name)}
    if isinstance(node, ast.If):
        return set().union(*(_names(item) for item in [*node.body, *node.orelse]))
    return set()


def _declaration(node: ast.stmt) -> ast.stmt | None:
    """Retain syntax that defines the API, without runtime bodies or docstrings."""
    node = copy.deepcopy(node)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        node.body = [ast.Pass()]
    elif isinstance(node, ast.ClassDef):
        node.body = [
            declaration
            for item in node.body
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            or not item.name.startswith("_")
            or item.name in _PROTOCOL_METHODS
            if (declaration := _declaration(item)) is not None
        ] or [ast.Pass()]
    elif isinstance(node, ast.If):
        node.body = [result for item in node.body if (result := _declaration(item)) is not None]
        node.orelse = [result for item in node.orelse if (result := _declaration(item)) is not None]
        if not node.body and not node.orelse:
            return None
        node.body = node.body or [ast.Pass()]
    elif not isinstance(node, (ast.AnnAssign, ast.Assign)):
        return None
    return node


def _references(declaration: ast.stmt) -> set[str]:
    names = {node.id for node in ast.walk(declaration) if isinstance(node, ast.Name)}
    annotations = []
    for node in ast.walk(declaration):
        if isinstance(node, (ast.arg, ast.AnnAssign)) and node.annotation is not None:
            annotations.append(node.annotation)
            if isinstance(node, ast.AnnAssign) and ast.unparse(node.annotation) == "TypeAlias":
                annotations.append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            annotations.append(node.returns)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"TypeVar", "TypeAliasType"}:
                annotations.extend(node.args[1:])
                annotations.extend(keyword.value for keyword in node.keywords)
    # Quoted forward references are types too; Literal strings remain values.
    while annotations:
        node = annotations.pop()
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            annotations.append(ast.parse(node.value, mode="eval").body)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value) in {
            "Literal",
            "typing.Literal",
        }:
            continue
        elif (
            isinstance(node, ast.Subscript)
            and ast.unparse(node.value) in {"Annotated", "typing.Annotated"}
            and isinstance(node.slice, ast.Tuple)
        ):
            annotations.append(node.slice.elts[0])
        elif node is not None:
            annotations.extend(ast.iter_child_nodes(node))
    return names


def _module_contract(
    module: ModuleType, exports: set[str] | None = None, *, include: set[str] | None = None
) -> dict[str, Any]:
    assert module.__file__ is not None
    tree = ast.parse(Path(module.__file__).read_text())
    source = module
    # Public modules explicitly re-export their implementation. Read declarations
    # from that one owner while checking bindings on the public module itself.
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            owner = importlib.util.resolve_name(
                "." * node.level + (node.module or ""), module.__package__
            )
            if owner.startswith("agents._runtime.") and _public_owner(owner) == module.__name__:
                source = importlib.import_module(owner)
                assert source.__file__ is not None
                tree = ast.parse(Path(source.__file__).read_text())
                break
    definitions = [(node, _names(node)) for node in tree.body if _names(node)]
    if exports is None:
        exports = (
            set(module.__all__)
            if hasattr(module, "__all__")
            else {name for _, names in definitions for name in names if not name.startswith("_")}
        )
    # Import availability is part of the contract; a source-only declaration is insufficient.
    bindings = {}
    for name in sorted(exports):
        value = getattr(module, name)
        # Python 3.14 gives union objects a typing.Union runtime identity.
        # The source declaration, not that interpreter detail, defines the alias.
        if get_origin(value) in (Union, UnionType):
            bindings[name] = name
        else:
            bindings[name] = (
                f"{value.__module__}.{value.__qualname__}"
                if hasattr(value, "__module__") and hasattr(value, "__qualname__")
                else name
            )
    selected = set(exports) | (include or set())
    declarations: list[ast.stmt] = []
    # Include base classes and aliases used by the public declarations. This captures
    # inherited field order/defaults without freezing unrelated private implementation.
    while True:
        declarations = [
            declaration
            for node, names in definitions
            if names & selected
            if (declaration := _declaration(node)) is not None
        ]
        referenced = set().union(*(_references(declaration) for declaration in declarations))
        expanded = selected | referenced
        if expanded == selected:
            break
        selected = expanded
    imports = []
    references = {}
    relative_groups: dict[int, list[int]] = {}
    import_statements = [
        (statement, group)
        for node in tree.body
        for statement, group in (
            [
                (node, 0),
                *((item, id(node.body)) for item in node.body),
                *((item, id(node.orelse)) for item in node.orelse),
            ]
            if isinstance(node, ast.If)
            else [(node, 0)]
        )
    ]
    for node, group in import_statements:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases = [
                alias
                for alias in node.names
                if (alias.asname or alias.name.split(".")[0]) in selected
            ]
            if aliases:
                node = copy.deepcopy(node)
                node.names = aliases
                if isinstance(node, ast.ImportFrom):
                    owner = importlib.util.resolve_name(
                        "." * node.level + (node.module or ""), source.__package__
                    )
                    owner = _public_owner(owner)
                    if source is not module and node.level:
                        # The runtime is one package level deeper. Preserve the
                        # reviewed logical import, including shared root leaves.
                        package = module.__package__ or ""
                        if owner.startswith(package + "."):
                            node.level = 1
                            node.module = owner[len(package) + 1 :]
                    if owner == "agents" or owner.startswith("agents."):
                        references.update(
                            (alias.asname or alias.name, f"{owner}.{alias.name}")
                            for alias in aliases
                        )
                imports.append(ast.unparse(node))
                if isinstance(node, ast.ImportFrom) and node.level:
                    relative_groups.setdefault(group, []).append(len(imports) - 1)
    if source is not module:
        # Moving shared leaves one level up changes isort's relative-import
        # grouping. Compare their logical public order after resolving owners.
        for positions in relative_groups.values():
            for index, statement in zip(
                positions, sorted(imports[index] for index in positions), strict=True
            ):
                imports[index] = statement
    return {
        "exports": bindings,
        "imports": imports,
        "references": references,
        "declarations": [ast.unparse(node) for node in declarations],
    }


def _public_owner(name: str) -> str:
    return name.replace("agents._runtime.", "agents.", 1)


def public_api() -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "src"))
    import agents

    root_exports = sorted(agents.__all__)
    public_modules = {
        f"agents.{path.stem}"
        for path in sorted((ROOT / "src/agents").glob("*.py"))
        if not path.stem.startswith("_") and path.stem != "version"
    }
    # Follow only imports used by retained declarations. A typing alias reports
    # its runtime owner as typing, so its source binding defines this boundary.
    required = {name: set() for name in public_modules | {"agents"}}
    while True:
        contracts = {
            name: _module_contract(
                importlib.import_module(name),
                set(root_exports)
                if name == "agents"
                else None
                if name in public_modules
                else set(),
                include=names,
            )
            for name, names in sorted(required.items())
        }
        expanded = {name: names.copy() for name, names in required.items()}
        for contract in contracts.values():
            for reference in contract["references"].values():
                owner, _, name = reference.rpartition(".")
                expanded.setdefault(owner, set()).add(name)
        if expanded == required:
            break
        required = expanded
    root = contracts.pop("agents")
    modules = {name: contracts.pop(name) for name in sorted(public_modules)}
    # The version value advances during release preparation; its import remains public.
    version = importlib.import_module("agents.version")
    if not isinstance(version.__version__, str):
        raise TypeError("agents.version.__version__ must be a string")
    modules["agents.version"] = {"exports": {"__version__": "str"}}
    bindings = root["exports"]
    for name, reference in root["references"].items():
        if name in root_exports:
            owner, _, source_name = reference.rpartition(".")
            if getattr(agents, name) is not getattr(importlib.import_module(owner), source_name):
                raise ValueError(f"Public binding agents.{name} differs from {reference}")
            bindings[name] = reference
    return {
        "root_exports": root_exports,
        "root_bindings": bindings,
        "root_imports": root["imports"],
        "root_declarations": root["declarations"],
        "modules": modules,
        "root_definitions": contracts,
    }


def _comparison_api(api: dict[str, Any] | None) -> dict[str, Any] | None:
    """Ignore explicit self-re-exports when comparing reviewed import syntax."""
    if api is None:
        return None
    result = copy.deepcopy(api)
    import_lists = [
        result["root_imports"],
        *(contract.get("imports", []) for contract in result["modules"].values()),
        *(contract.get("imports", []) for contract in result["root_definitions"].values()),
    ]
    for imports in import_lists:
        for index, statement in enumerate(imports):
            node = ast.parse(statement).body[0]
            assert isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names:
                if alias.asname == alias.name:
                    alias.asname = None
            imports[index] = ast.unparse(node)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.check and args.output is not None:
        parser.error("--check and --output cannot be combined")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    if args.version != project["version"] or not re.fullmatch(
        r"\d+\.\d+(?:\.\d+)*(?:[A-Za-z0-9.-]+)?", args.version
    ):
        parser.error("--version must match the version in pyproject.toml")
    if args.output is not None and args.output.resolve() == CONTRACT.resolve():
        parser.error("use the default output to update the committed release snapshot")
    api = public_api()
    old = json.loads(CONTRACT.read_text()) if CONTRACT.is_file() else {}
    if args.check:
        # Release versions and commit IDs change independently of the supported API.
        # Check the committed baseline before CI is allowed to regenerate that metadata.
        if _comparison_api(old.get("public_api")) != _comparison_api(api):
            print("Native public API differs from the reviewed snapshot.", file=sys.stderr)
            return 1
        return 0
    snapshot = {
        "baseline": f"v{args.version}",
        "baseline_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "public_api": api,
        "native_revision": (ROOT / "packages/codex/CODEX_REVISION").read_text().strip(),
    }
    if all(old.get(key) == value for key, value in snapshot.items() if key != "baseline_commit"):
        snapshot["baseline_commit"] = old["baseline_commit"]
    output = args.output or CONTRACT
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
