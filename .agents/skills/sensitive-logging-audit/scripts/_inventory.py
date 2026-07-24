from __future__ import annotations

import ast
import hashlib
import importlib.util
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

LOG_METHODS = {
    "critical",
    "debug",
    "error",
    "exception",
    "fatal",
    "info",
    "log",
    "warn",
    "warning",
}
SDK_LOGGER_MODULES = {"agents.logger", "agents.tracing.logger"}
POLICY_MODULE = "agents._debug"
POLICY_NAMES = {
    "DONT_LOG_MODEL_DATA": "model",
    "DONT_LOG_TOOL_DATA": "tool",
}
KNOWN_HELPERS = {
    "agents.logger.log_model_action_debug": "model",
    "agents.logger.log_model_action_error": "model",
    "agents.logger.log_model_action_warning": "model",
    "agents.logger.log_model_and_tool_action_debug": "model+tool",
    "agents.logger.log_model_and_tool_action_error": "model+tool",
    "agents.logger.log_model_and_tool_action_warning": "model+tool",
    "agents.logger.log_tool_action_debug": "tool",
    "agents.logger.log_tool_action_error": "tool",
    "agents.logger.log_tool_action_warning": "tool",
    "agents.run_internal.tool_execution.log_tool_action_error": "tool",
}
SENSITIVE_HELPER_METHODS = {name.rsplit(".", 1)[-1] for name in KNOWN_HELPERS}
RAW_MODULE_METHODS = {
    "builtins": {"print"},
    "os": {"write"},
    "pprint": {"pp", "pprint"},
    "traceback": {"print_exc", "print_exception"},
    "warnings": {"warn", "warn_explicit"},
}
DISPOSITIONS = {
    "intentional-output",
    "model",
    "model+tool",
    "operational",
    "tool",
    "uncertain",
}
COMPARISON_CLASSIFICATION_FIELDS = (
    "kind",
    "confidence",
    "method",
    "shape",
    "policy",
    "catch_value",
)

DefinitionValue = ast.AST | frozenset[str] | None


def _helper_fact(full_name: str) -> str:
    return f"helper:{KNOWN_HELPERS[full_name]}:{full_name.rsplit('.', 1)[-1]}"


def _is_sdk_logger_module_or_parent(module: str) -> bool:
    return any(
        candidate == module or candidate.startswith(f"{module}.")
        for candidate in SDK_LOGGER_MODULES
    )


@dataclass
class Finding:
    group_fingerprint: str
    fingerprint: str
    file: str
    line: int
    column: int
    kind: str
    confidence: str
    method: str
    shape: str
    policy: str
    catch_value: str | None
    context: str
    signals: list[str]
    call: str
    site_index: int = 0
    group_count: int = 1
    identity_quality: str = "unique"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_path(path: str | Path) -> str:
    return str(path).replace("\\", "/")


def collect_source_files(roots: Sequence[str | Path]) -> list[Path]:
    files: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).resolve()
        if root.is_file():
            if root.suffix == ".py":
                files.add(root)
            continue
        if not root.is_dir():
            raise FileNotFoundError(f"Inventory root does not exist: {root_value}")
        for path in root.rglob("*.py"):
            relative_parts = path.relative_to(root).parts
            if any(part.startswith(".") or part == "__pycache__" for part in relative_parts):
                continue
            files.add(path.resolve())
    return sorted(files)


def module_identity(file_path: str) -> tuple[str, str]:
    path = normalize_path(file_path)
    marker = "/src/"
    relative = path.split(marker, 1)[1] if marker in path else path.removeprefix("src/")
    parts = relative.split("/")
    if parts[-1] == "__init__.py":
        module = ".".join(parts[:-1])
        return module, module
    parts[-1] = parts[-1].removesuffix(".py")
    module = ".".join(parts)
    package = module.rsplit(".", 1)[0] if "." in module else module
    return module, package


def resolve_import(module: str | None, level: int, package: str) -> str:
    if level == 0:
        return module or ""
    name = "." * level + (module or "")
    try:
        return importlib.util.resolve_name(name, package)
    except (ImportError, ValueError):
        return name


def expression_key(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        receiver = expression_key(node.value)
        return f"{receiver}.{node.attr}" if receiver else None
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
        receiver = expression_key(node.value)
        if receiver and isinstance(node.slice.value, str | int):
            return f"{receiver}[{node.slice.value!r}]"
    return None


def target_keys(node: ast.AST) -> list[str]:
    key = expression_key(node)
    if key:
        return [key]
    if isinstance(node, ast.Starred):
        return target_keys(node.value)
    if isinstance(node, ast.List | ast.Tuple):
        return [key for element in node.elts for key in target_keys(element)]
    return []


def target_value_pairs(target: ast.AST, value: ast.AST) -> list[tuple[str, ast.AST | None]]:
    key = expression_key(target)
    if key:
        return [(key, value)]
    if isinstance(target, ast.Starred):
        return [(key, None) for key in target_keys(target.value)]
    if isinstance(target, ast.List | ast.Tuple):
        if isinstance(value, ast.List | ast.Tuple) and len(target.elts) == len(value.elts):
            return [
                pair
                for target_element, value_element in zip(target.elts, value.elts, strict=True)
                for pair in target_value_pairs(target_element, value_element)
            ]
        return [(key, None) for key in target_keys(target)]
    return []


def pattern_bound_names(pattern: ast.pattern) -> set[str]:
    if isinstance(pattern, ast.MatchAs):
        names = pattern_bound_names(pattern.pattern) if pattern.pattern is not None else set()
        if pattern.name:
            names.add(pattern.name)
        return names
    if isinstance(pattern, ast.MatchStar):
        return {pattern.name} if pattern.name else set()
    if isinstance(pattern, ast.MatchMapping):
        names = {name for child in pattern.patterns for name in pattern_bound_names(child)}
        if pattern.rest:
            names.add(pattern.rest)
        return names
    if isinstance(pattern, ast.MatchClass):
        return {
            name
            for child in [*pattern.patterns, *pattern.kwd_patterns]
            for name in pattern_bound_names(child)
        }
    if isinstance(pattern, ast.MatchSequence | ast.MatchOr):
        return {name for child in pattern.patterns for name in pattern_bound_names(child)}
    return set()


def iter_definitions(
    tree: ast.AST,
) -> Iterable[tuple[ast.AST, list[tuple[str, ast.AST | None]]]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            yield node, [(node.name, None)]
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                yield node, target_value_pairs(target, node.value)
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            yield node, target_value_pairs(node.target, node.value)
        elif isinstance(node, ast.AugAssign):
            yield node, [(key, None) for key in target_keys(node.target)]
        elif isinstance(node, ast.NamedExpr):
            yield node, target_value_pairs(node.target, node.value)
        elif isinstance(node, ast.For | ast.AsyncFor):
            yield node, [(key, None) for key in target_keys(node.target)]
        elif isinstance(node, ast.With | ast.AsyncWith):
            targets = [
                (key, None)
                for item in node.items
                if item.optional_vars is not None
                for key in target_keys(item.optional_vars)
            ]
            if targets:
                yield node, targets
        elif isinstance(node, ast.ExceptHandler) and node.name:
            yield node, [(node.name, None)]
        elif isinstance(node, ast.Delete):
            yield node, [(key, None) for target in node.targets for key in target_keys(target)]
        elif isinstance(node, ast.match_case):
            names = pattern_bound_names(node.pattern)
            if names:
                yield node.pattern, [(name, None) for name in sorted(names)]


def bound_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Starred):
        return bound_names(node.value)
    if isinstance(node, ast.List | ast.Tuple):
        return {name for element in node.elts for name in bound_names(element)}
    return set()


class Facts:
    def __init__(self, tree: ast.Module, file_path: str):
        self.values: dict[ast.AST, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        self.bindings: dict[ast.AST, set[str]] = defaultdict(set)
        self.node_scopes: dict[ast.AST, ast.AST] = {}
        self.scope_parents: dict[ast.AST, ast.AST | None] = {tree: None}
        self.parents = make_parent_map(tree)
        self.definitions: list[tuple[ast.AST, list[tuple[str, DefinitionValue]]]] = [
            (definition, list(targets)) for definition, targets in iter_definitions(tree)
        ]
        self.definition_values: dict[ast.AST, dict[str, list[tuple[ast.AST, DefinitionValue]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        self._policy_lookup_stack: set[tuple[int, str]] = set()
        self.module_name, self.package_name = module_identity(file_path)
        self._index_scopes(tree)
        self._collect_bindings(tree)
        for definition, targets in self.definitions:
            scope = self._scope_for(definition)
            for key, value in targets:
                self.definition_values[scope][key].append((definition, value))
        self._collect_imports(tree)
        self._resolve_definitions(tree)

    def _index_scopes(self, tree: ast.Module) -> None:
        scope_types = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda | ast.ClassDef

        def visit(node: ast.AST, scope: ast.AST) -> None:
            self.node_scopes[node] = scope
            for child in ast.iter_child_nodes(node):
                if isinstance(child, scope_types):
                    self.node_scopes[child] = scope
                    parent_scope = (
                        self.scope_parents[scope] if isinstance(scope, ast.ClassDef) else scope
                    )
                    self.scope_parents[child] = parent_scope
                    for descendant in ast.iter_child_nodes(child):
                        visit(descendant, child)
                else:
                    visit(child, scope)

        visit(tree, tree)

    def _scope_for(self, node: ast.AST) -> ast.AST:
        return self.node_scopes[node]

    def _collect_bindings(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            scope = self._scope_for(node)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                self.bindings[scope].add(node.name)
            if isinstance(node, ast.arg):
                self.bindings[scope].add(node.arg)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    self.bindings[scope].update(bound_names(target))
            elif isinstance(node, ast.AnnAssign | ast.AugAssign | ast.NamedExpr):
                self.bindings[scope].update(bound_names(node.target))
            elif isinstance(node, ast.For | ast.AsyncFor):
                self.bindings[scope].update(bound_names(node.target))
            elif isinstance(node, ast.comprehension):
                self.bindings[scope].update(bound_names(node.target))
            elif isinstance(node, ast.With | ast.AsyncWith):
                for item in node.items:
                    if item.optional_vars is not None:
                        self.bindings[scope].update(bound_names(item.optional_vars))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self.bindings[scope].add(node.name)
            elif isinstance(node, ast.match_case):
                self.bindings[scope].update(pattern_bound_names(node.pattern))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.bindings[scope].add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        self.bindings[scope].add(alias.asname or alias.name)

    def add(self, scope: ast.AST, key: str, values: Iterable[str]) -> bool:
        before = len(self.values[scope][key])
        self.values[scope][key].update(values)
        return len(self.values[scope][key]) != before

    def _lookup(self, node: ast.AST, key: str) -> set[str]:
        scope: ast.AST | None = self._scope_for(node)
        root = key.split(".", 1)[0].split("[", 1)[0]
        while scope is not None:
            if key in self.values[scope]:
                result = set(self.values[scope][key])
                result = {fact for fact in result if not fact.startswith("policy")}
                result.update(self._lookup_policy(node, key))
                return result
            if root in self.bindings[scope]:
                return self._lookup_policy(node, key)
            scope = self.scope_parents[scope]
        return set()

    def _lookup_policy(self, node: ast.AST, key: str) -> set[str]:
        token = (id(node), key)
        if token in self._policy_lookup_stack:
            return set()

        use_scope = self._scope_for(node)
        scope: ast.AST | None = use_scope
        root = key.split(".", 1)[0].split("[", 1)[0]
        while scope is not None:
            definitions = self.definition_values[scope].get(key, [])
            if definitions:
                if scope is not use_scope:
                    if all(isinstance(value, frozenset) for _, value in definitions):
                        _, imported_facts = max(
                            definitions, key=lambda item: self._node_position(item[0])
                        )
                        return set(imported_facts)
                    return set()
                preceding = [
                    definition
                    for definition in definitions
                    if self._node_position(definition[0]) < self._node_position(node)
                ]
                if not preceding:
                    return set()
                assignment, value = max(preceding, key=lambda item: self._node_position(item[0]))
                if not self._definition_precedes_in_same_block(assignment, node):
                    return set()
                if value is None:
                    return set()
                if isinstance(value, frozenset):
                    return set(value)
                self._policy_lookup_stack.add(token)
                try:
                    return {
                        fact
                        for fact in self.infer(value)
                        if fact.startswith(("policy:", "policy-exact:"))
                    }
                finally:
                    self._policy_lookup_stack.remove(token)

            if root in self.bindings[scope]:
                return set()
            scope = self.scope_parents[scope]
        return set()

    @staticmethod
    def _node_position(node: ast.AST) -> tuple[int, int]:
        return (getattr(node, "lineno", -1), getattr(node, "col_offset", -1))

    def _definition_precedes_in_same_block(self, definition: ast.AST, use: ast.AST) -> bool:
        if isinstance(definition, ast.For | ast.AsyncFor | ast.With | ast.AsyncWith):
            child = use
            while child in self.parents and self.parents[child] is not definition:
                child = self.parents[child]
            if child in definition.body:
                return True
            if isinstance(definition, ast.For | ast.AsyncFor) and child in definition.orelse:
                return True
        elif isinstance(definition, ast.ExceptHandler):
            child = use
            while child in self.parents and self.parents[child] is not definition:
                child = self.parents[child]
            if child in definition.body:
                return True
        elif isinstance(definition, ast.pattern):
            case = self.parents.get(definition)
            if isinstance(case, ast.match_case):
                child = use
                while child in self.parents and self.parents[child] is not case:
                    child = self.parents[child]
                if child is case.guard or child in case.body:
                    return True

        parent = self.parents.get(definition)
        if parent is None:
            return False
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list) or definition not in value:
                continue
            definition_index = value.index(definition)
            child = use
            while child in self.parents and self.parents[child] is not parent:
                child = self.parents[child]
            return child in value and definition_index < value.index(child)
        return False

    def _collect_imports(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            scope = self._scope_for(node)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                full_name = f"{self.module_name}.{node.name}"
                if scope is tree and full_name in KNOWN_HELPERS:
                    self.add(scope, node.name, {_helper_fact(full_name)})
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    bound = alias.asname or alias.name.split(".", 1)[0]
                    self._record_import_definition(node, scope, bound, None)
                    if alias.name in {
                        "builtins",
                        "logging",
                        "os",
                        "pprint",
                        "sys",
                        "traceback",
                        "warnings",
                    }:
                        self.add(scope, bound, {f"module:{alias.name}"})
                    elif alias.name == POLICY_MODULE:
                        self.add(
                            scope,
                            alias.asname or alias.name,
                            {f"module:{POLICY_MODULE}"},
                        )
                    elif alias.name in SDK_LOGGER_MODULES:
                        imported_module = (
                            alias.name if alias.asname else alias.name.split(".", 1)[0]
                        )
                        self.add(scope, bound, {f"module:{imported_module}"})
            elif isinstance(node, ast.ImportFrom):
                module = resolve_import(node.module, node.level, self.package_name)
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    bound = alias.asname or alias.name
                    full_name = f"{module}.{alias.name}" if module else alias.name
                    facts: set[str] = set()
                    policy_value: DefinitionValue = None
                    if module == "logging":
                        if alias.name == "getLogger":
                            facts.add("factory:logger")
                        elif alias.name in LOG_METHODS:
                            facts.add(f"method:logger:{alias.name}")
                        elif alias.name in {"Logger", "LoggerAdapter"}:
                            facts.add("type:logger")
                    if module == "functools" and alias.name == "partial":
                        facts.add("factory:partial")
                    if module in SDK_LOGGER_MODULES and alias.name == "logger":
                        facts.add("logger")
                    if full_name in SDK_LOGGER_MODULES:
                        facts.add(f"module:{full_name}")
                    if full_name == POLICY_MODULE:
                        facts.add(f"module:{POLICY_MODULE}")
                    if module == POLICY_MODULE and alias.name in POLICY_NAMES:
                        policy = POLICY_NAMES[alias.name]
                        policy_facts = {f"policy:{policy}", f"policy-exact:{policy}"}
                        facts.update(policy_facts)
                        policy_value = frozenset(policy_facts)
                    self._record_import_definition(node, scope, bound, policy_value)
                    if full_name in KNOWN_HELPERS:
                        facts.add(_helper_fact(full_name))
                    if module in RAW_MODULE_METHODS and alias.name in RAW_MODULE_METHODS[module]:
                        facts.add(f"method:raw:{module}.{alias.name}")
                    if module == "sys" and alias.name in {"stdout", "stderr"}:
                        facts.add(f"stream:{alias.name}")
                    if facts:
                        self.add(scope, bound, facts)

    def _record_import_definition(
        self,
        node: ast.Import | ast.ImportFrom,
        scope: ast.AST,
        bound: str,
        value: DefinitionValue,
    ) -> None:
        self.definitions.append((node, [(bound, value)]))
        self.definition_values[scope][bound].append((node, value))

    def infer(self, node: ast.AST) -> set[str]:
        key = expression_key(node)
        result = self._lookup(node, key) if key else set()
        if isinstance(node, ast.Name):
            if node.id == "print":
                result.add("method:raw:print")
            return result
        if isinstance(node, ast.Attribute):
            receiver_facts = self.infer(node.value)
            for fact in receiver_facts:
                if fact == "module:logging":
                    if node.attr == "getLogger":
                        result.add("factory:logger")
                    elif node.attr in {"Logger", "LoggerAdapter"}:
                        result.add("type:logger")
                    elif node.attr in LOG_METHODS:
                        result.add(f"method:logger:{node.attr}")
                elif fact == "logger" and node.attr in LOG_METHODS:
                    result.add(f"method:logger:{node.attr}")
                elif fact == f"module:{POLICY_MODULE}" and node.attr in POLICY_NAMES:
                    policy = POLICY_NAMES[node.attr]
                    result.update({f"policy:{policy}", f"policy-exact:{policy}"})
                elif fact.startswith("module:"):
                    module = fact.split(":", 1)[1]
                    qualified_name = f"{module}.{node.attr}"
                    if _is_sdk_logger_module_or_parent(qualified_name):
                        result.add(f"module:{qualified_name}")
                    if module in SDK_LOGGER_MODULES:
                        helper_policy = KNOWN_HELPERS.get(qualified_name)
                        if helper_policy is not None:
                            result.add(_helper_fact(qualified_name))
                        if node.attr == "logger":
                            result.add("logger")
                    if module in RAW_MODULE_METHODS and node.attr in RAW_MODULE_METHODS[module]:
                        result.add(f"method:raw:{module}.{node.attr}")
                    if module == "sys" and node.attr in {"stdout", "stderr"}:
                        result.add(f"stream:{node.attr}")
                elif fact.startswith("stream:"):
                    if node.attr == "buffer":
                        result.add(fact)
                    elif node.attr in {"write", "writelines"}:
                        stream = fact.split(":", 1)[1]
                        result.add(f"method:raw:{stream}.{node.attr}")
            return result
        if isinstance(node, ast.Call):
            callee_facts = self.infer(node.func)
            if "factory:logger" in callee_facts or "type:logger" in callee_facts:
                result.add("logger")
            if self._is_getattr_call(node) and len(node.args) >= 2:
                receiver_facts = self.infer(node.args[0])
                attribute = node.args[1]
                if (
                    "logger" in receiver_facts
                    and isinstance(attribute, ast.Constant)
                    and isinstance(attribute.value, str)
                    and attribute.value in LOG_METHODS
                ):
                    result.add(f"method:logger:{attribute.value}")
            if self._is_partial(callee_facts) and node.args:
                callable_facts = self.infer(node.args[0])
                method_facts = {fact for fact in callable_facts if fact.startswith("method:")}
                result.update(method_facts)
                inherited_shape = any(fact.startswith("partial-shape:") for fact in callable_facts)
                if len(node.args) == 1 and not node.keywords and not inherited_shape:
                    result.add("partial-shape:empty")
                else:
                    bound_call = ast.Call(
                        func=node.args[0],
                        args=node.args[1:],
                        keywords=node.keywords,
                    )
                    for method_fact in method_facts:
                        _, _, method = method_fact.split(":", 2)
                        shape = call_shape_with_partial(bound_call, method, callable_facts)
                        result.add(f"partial-shape:{shape}")
            return result
        if isinstance(node, ast.BoolOp):
            for value in node.values:
                result.update(self.infer(value))
            result = {fact for fact in result if not fact.startswith("policy-exact:")}
            return result
        if isinstance(node, ast.IfExp):
            result.update(self.infer(node.body))
            result.update(self.infer(node.orelse))
            result = {fact for fact in result if not fact.startswith("policy-exact:")}
            return result
        if isinstance(node, ast.UnaryOp):
            result.update(self.infer(node.operand))
            return {fact for fact in result if not fact.startswith("policy-exact:")}
        if isinstance(node, ast.Compare):
            result.update(self.infer(node.left))
            for comparator in node.comparators:
                result.update(self.infer(comparator))
            return {fact for fact in result if not fact.startswith("policy-exact:")}
        if isinstance(node, ast.Tuple | ast.List | ast.Set):
            for element in node.elts:
                result.update(self.infer(element))
            return result
        return result

    @staticmethod
    def _is_partial(facts: set[str]) -> bool:
        return "factory:partial" in facts

    def _is_getattr_call(self, node: ast.Call) -> bool:
        if isinstance(node.func, ast.Name):
            return node.func.id == "getattr"
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "getattr"
            and "module:builtins" in self.infer(node.func.value)
        )

    def _resolve_definitions(self, tree: ast.Module) -> None:
        self._seed_special_attributes(tree)
        changed = True
        while changed:
            changed = False
            for definition, targets in self.definitions:
                scope = self._scope_for(definition)
                for key, value in targets:
                    if isinstance(value, frozenset):
                        inferred = value
                    else:
                        inferred = self.infer(value) if value is not None else set()
                    changed |= self.add(scope, key, inferred)

    def _seed_special_attributes(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Import):
                continue
            scope = self._scope_for(node)
            for alias in node.names:
                if alias.name == "functools":
                    bound = alias.asname or "functools"
                    self.add(scope, f"{bound}.partial", {"factory:partial"})


def make_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}


def normalize_node(node: ast.AST, source: str) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        segment = ast.dump(node, annotate_fields=True, include_attributes=False)
    return re.sub(r"\s+", " ", segment).strip()


def node_references_name(node: ast.AST, name: str) -> bool:
    return any(isinstance(child, ast.Name) and child.id == name for child in ast.walk(node))


def branch_for_child(parent: ast.AST, child: ast.AST) -> tuple[ast.AST, bool] | None:
    if isinstance(parent, ast.If):
        if child in parent.body:
            return parent.test, True
        if child in parent.orelse:
            return parent.test, False
    if isinstance(parent, ast.IfExp):
        if child is parent.body:
            return parent.test, True
        if child is parent.orelse:
            return parent.test, False
    return None


def possible_boolean_results(node: ast.AST, facts: Facts, policy: str, value: bool) -> set[bool]:
    if f"policy-exact:{policy}" in facts.infer(node):
        key = expression_key(node)
        if key or isinstance(node, ast.Name):
            return {value}
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return {node.value}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return {not item for item in possible_boolean_results(node.operand, facts, policy, value)}
    if isinstance(node, ast.BoolOp):
        child_results = [
            possible_boolean_results(item, facts, policy, value) for item in node.values
        ]
        outcomes = {False, True}
        if isinstance(node.op, ast.And):
            outcomes = {all(values) for values in _boolean_product(child_results)}
        elif isinstance(node.op, ast.Or):
            outcomes = {any(values) for values in _boolean_product(child_results)}
        return outcomes
    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
        and isinstance(node.ops[0], ast.Eq | ast.NotEq | ast.Is | ast.IsNot)
    ):
        left = possible_boolean_results(node.left, facts, policy, value)
        right = possible_boolean_results(node.comparators[0], facts, policy, value)
        equality = isinstance(node.ops[0], ast.Eq | ast.Is)
        return {(a == b) if equality else (a != b) for a in left for b in right}
    return {False, True}


def _boolean_product(values: Sequence[set[bool]]) -> list[tuple[bool, ...]]:
    products: list[tuple[bool, ...]] = [()]
    for options in values:
        products = [prefix + (option,) for prefix in products for option in options]
    return products


def block_always_exits(statements: Sequence[ast.stmt]) -> bool:
    if not statements:
        return False
    final = statements[-1]
    if isinstance(final, ast.Return | ast.Raise | ast.Break | ast.Continue):
        return True
    if isinstance(final, ast.If):
        return block_always_exits(final.body) and block_always_exits(final.orelse)
    return False


def continuation_guards(
    parent: ast.AST,
    child: ast.AST,
    facts: Facts,
) -> set[str]:
    guards: set[str] = set()
    for field_name in ("body", "orelse", "finalbody"):
        block = getattr(parent, field_name, None)
        if not isinstance(block, list) or child not in block:
            continue
        child_index = block.index(child)
        for statement in block[:child_index]:
            if not isinstance(statement, ast.If):
                continue
            body_exits = block_always_exits(statement.body)
            else_exits = block_always_exits(statement.orelse)
            if body_exits == else_exits:
                continue
            continuing_branch = not body_exits
            for policy in ("model", "tool"):
                has_policy = any(
                    f"policy:{policy}" in facts.infer(candidate)
                    for candidate in ast.walk(statement.test)
                )
                if has_policy and continuing_branch not in possible_boolean_results(
                    statement.test, facts, policy, True
                ):
                    guards.add(policy)
    return guards


def guarded_policy(node: ast.AST, parents: Mapping[ast.AST, ast.AST], facts: Facts) -> str:
    guards: set[str] = set()
    child = node
    current = parents.get(node)
    while current is not None:
        branch = branch_for_child(current, child)
        if branch:
            condition, branch_value = branch
            for policy in ("model", "tool"):
                if any(
                    f"policy:{policy}" in facts.infer(candidate)
                    for candidate in ast.walk(condition)
                ) and branch_value not in possible_boolean_results(condition, facts, policy, True):
                    guards.add(policy)
        guards.update(continuation_guards(current, child, facts))
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            break
        child = current
        current = parents.get(current)
    if guards == {"model", "tool"}:
        return "model+tool-guard"
    if "model" in guards:
        return "model-guard"
    if "tool" in guards:
        return "tool-guard"
    return "none"


def call_site_context(node: ast.AST, parents: Mapping[ast.AST, ast.AST], source: str) -> str:
    parts: list[str] = []
    child = node
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            parts.append(f"{type(current).__name__}:{current.name}")
        elif isinstance(current, ast.If):
            branch = (
                "body" if child in current.body else "else" if child in current.orelse else "test"
            )
            parts.append(f"if:{normalize_node(current.test, source)}:{branch}")
        elif isinstance(current, ast.IfExp):
            branch = (
                "body" if child is current.body else "else" if child is current.orelse else "test"
            )
            parts.append(f"ifexp:{normalize_node(current.test, source)}:{branch}")
        elif isinstance(current, ast.Try):
            if child in current.body:
                branch = "try"
            elif child in current.handlers:
                handler_index = next(
                    index for index, handler in enumerate(current.handlers) if handler is child
                )
                branch = f"handler:{handler_index}"
            elif child in current.orelse:
                branch = "else"
            elif child in current.finalbody:
                branch = "finally"
            else:
                branch = "nested"
            parts.append(f"try:{branch}")
        elif isinstance(current, ast.ExceptHandler):
            parts.append(
                f"except:{normalize_node(current.type, source) if current.type else 'bare'}"
            )
        elif isinstance(current, ast.match_case):
            pattern = normalize_node(current.pattern, source)
            guard = normalize_node(current.guard, source) if current.guard else ""
            parts.append(f"case:{pattern}:{guard}")
        elif isinstance(current, ast.Match):
            parts.append(f"match:{normalize_node(current.subject, source)}")
        elif isinstance(current, ast.For | ast.AsyncFor | ast.While):
            branch = (
                "body" if child in current.body else "else" if child in current.orelse else "header"
            )
            parts.append(f"{type(current).__name__}:{branch}")
        elif isinstance(current, ast.Call):
            if child in current.args:
                argument_index = next(
                    index for index, argument in enumerate(current.args) if argument is child
                )
                parts.append(f"callback:{normalize_node(current.func, source)}:{argument_index}")
            elif isinstance(child, ast.keyword) and child in current.keywords:
                keyword_name = child.arg if child.arg is not None else "**"
                parts.append(
                    f"callback:{normalize_node(current.func, source)}:keyword:{keyword_name}"
                )
        child = current
        current = parents.get(current)
    return ">".join(reversed(parts)) or "<module>"


def enclosing_catch_names(node: ast.AST, parents: Mapping[ast.AST, ast.AST]) -> list[str]:
    names: list[str] = []
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.ExceptHandler) and current.name:
            names.append(current.name)
        current = parents.get(current)
    return names


def call_shape(call: ast.Call, method: str) -> str:
    if any(keyword.arg is None for keyword in call.keywords):
        return "payload"
    keywords = {keyword.arg: keyword.value for keyword in call.keywords if keyword.arg is not None}
    exc_info = keywords.get("exc_info")
    has_exc_info = exc_info is not None and not (
        isinstance(exc_info, ast.Constant) and exc_info.value in (None, False)
    )
    if method in {"exception", "traceback.print_exc"} or has_exc_info:
        return "exception-payload"
    if len(call.args) > 1 or "extra" in keywords:
        return "payload"
    if method in SENSITIVE_HELPER_METHODS and any(
        name not in {"logger", "target_logger", "message", "msg"} for name in keywords
    ):
        return "payload"
    message = call.args[0] if call.args else keywords.get("msg", keywords.get("message"))
    if message is None:
        if call.keywords:
            return "dynamic-message"
        return "static-message"
    if isinstance(message, ast.Constant) and isinstance(message.value, str):
        return "static-message"
    return "dynamic-message"


def call_shape_with_partial(call: ast.Call, method: str, callee_facts: set[str]) -> str:
    shape = call_shape(call, method)
    bound_shapes = {
        fact.split(":", 1)[1] for fact in callee_facts if fact.startswith("partial-shape:")
    }
    if not bound_shapes or bound_shapes == {"empty"}:
        return shape
    if "exception-payload" in bound_shapes or shape == "exception-payload":
        return "exception-payload"
    if "payload" in bound_shapes or shape == "payload":
        return "payload"

    has_call_arguments = bool(call.args or call.keywords)
    if "dynamic-message" in bound_shapes:
        return "payload" if has_call_arguments else "dynamic-message"
    if "static-message" in bound_shapes:
        return "payload" if has_call_arguments else "static-message"
    return shape


def is_standard_file_descriptor_write(call: ast.Call, method: str) -> bool:
    if method != "os.write":
        return True
    if not call.args:
        return False
    descriptor = call.args[0]
    return (
        isinstance(descriptor, ast.Constant)
        and type(descriptor.value) is int
        and descriptor.value in {1, 2}
    )


def is_unknown_logging_callback(argument: ast.AST, keyword: str | None) -> bool:
    if not isinstance(argument, ast.Attribute) or argument.attr not in LOG_METHODS:
        return False
    receiver = expression_key(argument.value)
    receiver_name = receiver.rsplit(".", 1)[-1].lower() if receiver else ""
    logger_like_receiver = receiver_name in {"log", "logger"} or receiver_name.endswith(
        ("_log", "_logger")
    )
    callback_keyword = keyword is not None and (
        keyword.startswith("on_")
        or keyword.endswith(("_callback", "_handler"))
        or keyword in {"callback", "handler"}
    )
    return logger_like_receiver or callback_keyword


def signals_for(text: str) -> list[str]:
    normalized = text.lower()
    groups = [
        ("model", r"\b(model|response|request|completion|llm|realtime event)\b"),
        (
            "tool",
            r"\b(tool|function call|arguments|computer action|shell action|apply_patch|mcp)\b",
        ),
        ("error", r"\b(error|err|exception|failure|failed|reason|traceback)\b"),
        ("payload", r"\b(input|output|item|event|payload|data|trace|span|extra)\b"),
    ]
    return [name for name, pattern in groups if re.search(pattern, normalized)]


def inventory_source(source: str, file_path: str = "fixture.py") -> list[Finding]:
    normalized_path = normalize_path(file_path)
    tree = ast.parse(source, filename=normalized_path)
    parents = make_parent_map(tree)
    facts = Facts(tree, normalized_path)
    findings: list[Finding] = []
    recorded_nodes: set[tuple[int, str, str]] = set()

    def record(
        node: ast.AST,
        call_text: str,
        kind: str,
        confidence: str,
        method: str,
        shape: str,
        policy: str,
        catch_value: str | None,
    ) -> None:
        key = (id(node), kind, method)
        if key in recorded_nodes:
            return
        recorded_nodes.add(key)
        context = call_site_context(node, parents, source)
        group = hashlib.sha256(f"{normalized_path}\0{context}\0{call_text}".encode()).hexdigest()[
            :12
        ]
        findings.append(
            Finding(
                group_fingerprint=group,
                fingerprint=group,
                file=normalized_path,
                line=getattr(node, "lineno", 1),
                column=getattr(node, "col_offset", 0) + 1,
                kind=kind,
                confidence=confidence,
                method=method,
                shape=shape,
                policy=policy,
                catch_value=catch_value,
                context=context,
                signals=signals_for(call_text),
                call=call_text,
            )
        )

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_text = normalize_node(node, source)
        callee_facts = facts.infer(node.func)
        sink_facts = sorted(
            fact for fact in callee_facts if fact.startswith(("method:", "helper:"))
        )
        catch_names = enclosing_catch_names(node, parents)
        referenced = [
            name
            for name in catch_names
            if any(
                node_references_name(value, name)
                for value in [*node.args, *(kw.value for kw in node.keywords)]
            )
        ]
        catch_value = ", ".join(referenced) or None
        policy = guarded_policy(node, parents, facts)

        if sink_facts:
            for fact in sink_facts:
                category, sink_kind, sink_method = _split_sink_fact(fact)
                if category == "helper":
                    helper_policy = f"{sink_kind}-helper"
                    record(
                        node,
                        call_text,
                        "sensitive-helper",
                        "confirmed",
                        sink_method,
                        call_shape(node, sink_method),
                        helper_policy,
                        catch_value,
                    )
                else:
                    if sink_kind == "logger":
                        shape = call_shape_with_partial(node, sink_method, callee_facts)
                        if shape == "exception-payload" and catch_value is None and catch_names:
                            catch_value = "active exception"
                        record(
                            node,
                            call_text,
                            "logger",
                            "confirmed",
                            sink_method,
                            shape,
                            policy,
                            catch_value,
                        )
                    else:
                        if not is_standard_file_descriptor_write(node, sink_method):
                            continue
                        record(
                            node,
                            call_text,
                            "raw-output",
                            "confirmed",
                            sink_method,
                            call_shape_with_partial(node, sink_method, callee_facts),
                            policy,
                            catch_value,
                        )
        elif isinstance(node.func, ast.Attribute) and node.func.attr in LOG_METHODS:
            shape = call_shape(node, node.func.attr)
            if shape == "exception-payload" and catch_value is None and catch_names:
                catch_value = "active exception"
            record(
                node,
                call_text,
                "logging-candidate",
                "unknown",
                node.func.attr,
                shape,
                policy,
                catch_value,
            )
        elif isinstance(node.func, ast.Name) and node.func.id in {
            "log_model_action_error",
            "log_tool_action_error",
        }:
            record(
                node,
                call_text,
                "helper-candidate",
                "unknown",
                node.func.id,
                call_shape(node, node.func.id),
                "none",
                catch_value,
            )

        if not _is_partial_call(callee_facts):
            callback_arguments = [
                *((argument, None) for argument in node.args),
                *(
                    (keyword.value, keyword.arg)
                    for keyword in node.keywords
                    if keyword.arg is not None
                ),
            ]
            for argument, keyword in callback_arguments:
                method_facts = {
                    fact for fact in facts.infer(argument) if fact.startswith("method:")
                }
                for fact in method_facts:
                    _, sink_kind, sink_method = fact.split(":", 2)
                    record(
                        argument,
                        normalize_node(argument, source),
                        "logger-callback" if sink_kind == "logger" else "raw-output-callback",
                        "confirmed",
                        sink_method,
                        "dynamic-message",
                        guarded_policy(argument, parents, facts)
                        if sink_kind == "logger"
                        else "none",
                        "callback value",
                    )
                if (
                    not method_facts
                    and is_unknown_logging_callback(argument, keyword)
                    and isinstance(argument, ast.Attribute)
                ):
                    record(
                        argument,
                        normalize_node(argument, source),
                        "logging-candidate-callback",
                        "unknown",
                        argument.attr,
                        "dynamic-message",
                        guarded_policy(argument, parents, facts),
                        "callback value",
                    )

    findings.sort(key=lambda finding: (finding.file, finding.line, finding.column, finding.kind))
    counts = Counter(finding.group_fingerprint for finding in findings)
    indexes: dict[str, int] = defaultdict(int)
    for finding in findings:
        index = indexes[finding.group_fingerprint]
        indexes[finding.group_fingerprint] += 1
        count = counts[finding.group_fingerprint]
        finding.site_index = index
        finding.group_count = count
        finding.identity_quality = "duplicate" if count > 1 else "unique"
        finding.fingerprint = (
            finding.group_fingerprint if count == 1 else f"{finding.group_fingerprint}:{index}"
        )
    return findings


def _split_sink_fact(fact: str) -> tuple[str, str, str]:
    if fact.startswith("helper:"):
        _, policy, method = fact.split(":", 2)
        return "helper", policy, method
    _, sink_kind, sink_method = fact.split(":", 2)
    return "method", sink_kind, sink_method


def _is_partial_call(callee_facts: set[str]) -> bool:
    return "factory:partial" in callee_facts


def summarize(findings: Sequence[Finding]) -> dict[str, int]:
    dynamic = [finding for finding in findings if finding.shape != "static-message"]
    duplicate_groups = {
        finding.group_fingerprint for finding in findings if finding.identity_quality == "duplicate"
    }
    return {
        "total": len(findings),
        "dynamic": len(dynamic),
        "unclassifiedDynamic": sum(finding.policy == "none" for finding in dynamic),
        "catchValueLogs": sum(finding.catch_value is not None for finding in findings),
        "unclassifiedCatchValueLogs": sum(
            finding.catch_value is not None and finding.policy == "none" for finding in findings
        ),
        "rawOutputCalls": sum(finding.kind.startswith("raw-output") for finding in findings),
        "unknownReceiverCalls": sum(finding.confidence == "unknown" for finding in findings),
        "duplicateGroups": len(duplicate_groups),
    }


def compare_findings(
    baseline: Sequence[Mapping[str, Any]], findings: Sequence[Finding]
) -> dict[str, Any]:
    before = Counter(str(item["group_fingerprint"]) for item in baseline)
    after = Counter(finding.group_fingerprint for finding in findings)
    before_classifications = _comparison_classifications(baseline)
    after_classifications = _comparison_classifications(findings)
    return {
        "new": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "countChanged": [
            {"group_fingerprint": key, "before": before[key], "after": after[key]}
            for key in sorted(before.keys() & after.keys())
            if before[key] != after[key]
        ],
        "classificationChanged": [
            {
                "group_fingerprint": key,
                "before": _render_classification_counts(before_classifications[key]),
                "after": _render_classification_counts(after_classifications[key]),
            }
            for key in sorted(before.keys() & after.keys())
            if before_classifications[key] != after_classifications[key]
        ],
    }


def _comparison_classifications(
    findings: Sequence[Mapping[str, Any] | Finding],
) -> dict[str, Counter[tuple[Any, ...]]]:
    result: dict[str, Counter[tuple[Any, ...]]] = defaultdict(Counter)
    for finding in findings:
        if isinstance(finding, Finding):
            group = finding.group_fingerprint
            classification = tuple(
                getattr(finding, field) for field in COMPARISON_CLASSIFICATION_FIELDS
            )
        else:
            group = str(finding["group_fingerprint"])
            classification = tuple(finding.get(field) for field in COMPARISON_CLASSIFICATION_FIELDS)
        result[group][classification] += 1
    return result


def _render_classification_counts(
    counts: Counter[tuple[Any, ...]],
) -> list[dict[str, Any]]:
    return [
        {
            **dict(zip(COMPARISON_CLASSIFICATION_FIELDS, classification, strict=True)),
            "count": count,
        }
        for classification, count in sorted(counts.items(), key=lambda item: repr(item[0]))
    ]


def validate_review_ledger(
    ledger: Mapping[str, Any], findings: Sequence[Finding]
) -> dict[str, Any]:
    reviews = ledger.get("reviews")
    if not isinstance(reviews, list):
        return {"valid": False, "errors": ["Review ledger must contain a reviews list."]}
    dynamic_counts = Counter(
        finding.group_fingerprint for finding in findings if finding.shape != "static-message"
    )
    review_groups = [
        str(review.get("group_fingerprint"))
        for review in reviews
        if isinstance(review, dict) and review.get("group_fingerprint")
    ]
    duplicate_review_groups = sorted(
        group for group, count in Counter(review_groups).items() if count > 1
    )
    errors = [f"Duplicate review entries for {group}" for group in duplicate_review_groups]
    review_by_group: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        if not isinstance(review, dict) or not review.get("group_fingerprint"):
            continue
        review_by_group.setdefault(str(review["group_fingerprint"]), review)
    for group, count in sorted(dynamic_counts.items()):
        review = review_by_group.get(group)
        if review is None:
            errors.append(f"Missing review for dynamic group {group}.")
            continue
        if review.get("disposition") not in DISPOSITIONS:
            errors.append(f"Invalid disposition for {group}.")
        evidence = review.get("evidence")
        if not isinstance(evidence, str) or not evidence.strip():
            errors.append(f"Missing evidence for {group}.")
        action = review.get("action")
        if not isinstance(action, str) or not action.strip():
            errors.append(f"Missing action for {group}.")
        if review.get("group_count") != count:
            actual = review.get("group_count")
            errors.append(f"Group count mismatch for {group}: expected {count}, got {actual}.")
    extras = sorted(review_by_group.keys() - dynamic_counts.keys())
    if extras:
        errors.append(f"Review ledger contains stale groups: {', '.join(extras)}.")
    return {"valid": not errors, "errors": errors}
