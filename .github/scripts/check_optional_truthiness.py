from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_CALLABLE_MODULES = {"collections.abc", "typing"}

FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef
ReferenceKind = Literal["callable", "class"]


@dataclass(frozen=True, order=True)
class Violation:
    path: Path
    line: int
    column: int
    expression: str

    def format(self) -> str:
        return (
            f"{self.path}:{self.line}:{self.column}: optional object uses truthiness: "
            f"{self.expression}"
        )


@dataclass
class _ClassInfo:
    fields: set[str] = field(default_factory=set)


@dataclass
class _FunctionInfo:
    node: FunctionNode
    owner: int | None
    signature_bindings: dict[str, ReferenceKind]
    body_bindings: dict[str, ReferenceKind]


@dataclass
class _ModuleInfo:
    path: Path
    classes: dict[int, _ClassInfo]
    functions: dict[int, _FunctionInfo]


def _walk_scope(body: list[ast.stmt]) -> Iterable[ast.AST]:
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(
            current,
            ast.FunctionDef
            | ast.AsyncFunctionDef
            | ast.ClassDef
            | ast.Lambda
            | ast.ListComp
            | ast.SetComp
            | ast.DictComp
            | ast.GeneratorExp,
        ):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _walk_comprehension_bindings(nodes: Iterable[ast.AST]) -> Iterable[ast.AST]:
    stack = list(reversed(list(nodes)))
    while stack:
        current = stack.pop()
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            continue
        if isinstance(current, ast.NamedExpr):
            yield current.target
            stack.append(current.value)
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _walk_scope_bindings(body: list[ast.stmt]) -> Iterable[ast.AST]:
    stack: list[ast.AST] = list(reversed(body))
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef):
            defining_expressions: list[ast.AST] = [
                *current.decorator_list,
                current.args,
            ]
            if current.returns is not None:
                defining_expressions.append(current.returns)
            stack.extend(reversed(defining_expressions))
            continue
        if isinstance(current, ast.ClassDef):
            defining_expressions = [
                *current.decorator_list,
                *current.bases,
                *(keyword.value for keyword in current.keywords),
            ]
            stack.extend(reversed(defining_expressions))
            continue
        if isinstance(current, ast.Lambda):
            stack.append(current.args)
            continue
        if isinstance(current, ast.ListComp | ast.SetComp | ast.DictComp | ast.GeneratorExp):
            first_generator, *remaining_generators = current.generators
            stack.append(first_generator.iter)
            nested_expressions: list[ast.AST] = [
                *(condition for generator in current.generators for condition in generator.ifs),
                *(generator.iter for generator in remaining_generators),
            ]
            if isinstance(current, ast.DictComp):
                nested_expressions.extend((current.key, current.value))
            else:
                nested_expressions.append(current.elt)
            yield from _walk_comprehension_bindings(nested_expressions)
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _walk_function(node: FunctionNode) -> Iterable[ast.AST]:
    yield from _walk_scope(node.body)


def _is_static_method(function: FunctionNode) -> bool:
    return any(
        isinstance(decorator, ast.Name)
        and decorator.id == "staticmethod"
        or isinstance(decorator, ast.Attribute)
        and isinstance(decorator.value, ast.Name)
        and decorator.value.id == "builtins"
        and decorator.attr == "staticmethod"
        for decorator in function.decorator_list
    )


def _owns_instance_fields(function: FunctionNode) -> bool:
    return not _is_static_method(function)


def _is_none_annotation(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and node.value is None
        or isinstance(node, ast.Name)
        and node.id == "None"
    )


def _optional_payload(annotation: ast.expr) -> ast.expr | None:
    if not isinstance(annotation, ast.BinOp) or not isinstance(annotation.op, ast.BitOr):
        return None
    members: list[ast.expr] = []

    def collect(node: ast.expr) -> None:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            collect(node.left)
            collect(node.right)
        else:
            members.append(node)

    collect(annotation)
    payloads = [member for member in members if not _is_none_annotation(member)]
    if len(payloads) != 1 or len(payloads) == len(members):
        return None
    return payloads[0]


def _is_direct_optional_reference(
    annotation: ast.expr,
    *,
    bindings: dict[str, ReferenceKind],
) -> bool:
    payload = _optional_payload(annotation)
    if payload is None:
        return False
    if (
        isinstance(payload, ast.Subscript)
        and isinstance(payload.value, ast.Name)
        and bindings.get(payload.value.id) == "callable"
    ):
        return True
    if not isinstance(payload, ast.Name):
        return False
    return bindings.get(payload.id) == "class"


def _reference_bindings_for_scope(
    body: list[ast.stmt],
    inherited_bindings: dict[str, ReferenceKind],
    *,
    arguments: Iterable[ast.arg] = (),
    forced_shadows: Iterable[str] = (),
    supported_classes: Mapping[int, str] | None = None,
) -> dict[str, ReferenceKind]:
    supported_classes = supported_classes or {}
    tracked_names = {*inherited_bindings, *supported_classes.values(), "Callable"}
    supported_bindings: dict[str, ReferenceKind] = {}
    shadowed_names = {
        name
        for name in [*(argument.arg for argument in arguments), *forced_shadows]
        if name in tracked_names
    }

    def record_supported(name: str, kind: ReferenceKind) -> None:
        existing = supported_bindings.get(name)
        if existing is not None:
            shadowed_names.add(name)
        else:
            supported_bindings[name] = kind

    for node in _walk_scope_bindings(body):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for imported in node.names:
                if isinstance(node, ast.ImportFrom) and imported.name == "*":
                    shadowed_names.update(tracked_names)
                    continue
                local_name = imported.asname or imported.name.split(".")[0]
                is_standard_callable = (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module in _CALLABLE_MODULES
                    and imported.name == "Callable"
                    and imported.asname is None
                )
                if is_standard_callable:
                    record_supported("Callable", "callable")
                elif local_name in tracked_names:
                    shadowed_names.add(local_name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if isinstance(node, ast.ClassDef) and id(node) in supported_classes:
                record_supported(node.name, "class")
            elif node.name in tracked_names:
                shadowed_names.add(node.name)
        elif (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, ast.Store | ast.Del)
            and node.id in tracked_names
        ):
            shadowed_names.add(node.id)
        elif (
            isinstance(node, ast.ExceptHandler)
            and isinstance(node.name, str)
            and node.name in tracked_names
        ):
            shadowed_names.add(node.name)
        elif isinstance(node, ast.MatchAs | ast.MatchStar) and node.name in tracked_names:
            shadowed_names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest in tracked_names:
            shadowed_names.add(node.rest)

    bindings = {
        name: kind for name, kind in inherited_bindings.items() if name not in shadowed_names
    }
    bindings.update(
        (name, kind) for name, kind in supported_bindings.items() if name not in shadowed_names
    )
    return bindings


def _cross_scope_declared_names(tree: ast.Module) -> set[str]:
    return {
        name
        for node in ast.walk(tree)
        if isinstance(node, ast.Global | ast.Nonlocal)
        for name in node.names
    }


def _type_parameter_names(node: FunctionNode | ast.ClassDef) -> set[str]:
    return {
        name
        for type_parameter in getattr(node, "type_params", ())
        if isinstance(name := getattr(type_parameter, "name", None), str)
    }


def _collect_module_info(path: Path, tree: ast.Module) -> _ModuleInfo:
    classes: dict[int, _ClassInfo] = {}
    functions: dict[int, _FunctionInfo] = {}
    cross_scope_shadows = _cross_scope_declared_names(tree)

    def is_simple_reference_class(node: ast.ClassDef) -> bool:
        return not node.bases or all(
            isinstance(base, ast.Name) and base.id == "object" for base in node.bases
        )

    supported_classes = {
        id(node): node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and is_simple_reference_class(node)
    }
    module_bindings = _reference_bindings_for_scope(
        tree.body,
        {},
        forced_shadows=cross_scope_shadows,
        supported_classes=supported_classes,
    )

    def register_function(
        function: FunctionNode,
        *,
        signature_bindings: dict[str, ReferenceKind],
        inherited_body_bindings: dict[str, ReferenceKind],
        owner: int | None,
    ) -> None:
        forced_shadows = cross_scope_shadows | _type_parameter_names(function)
        signature_bindings = {
            name: kind for name, kind in signature_bindings.items() if name not in forced_shadows
        }
        body_bindings = _reference_bindings_for_scope(
            function.body,
            inherited_body_bindings,
            arguments=_arguments(function),
            forced_shadows=forced_shadows,
        )
        functions[id(function)] = _FunctionInfo(
            node=function,
            owner=owner,
            signature_bindings=signature_bindings,
            body_bindings=body_bindings,
        )
        if owner is not None and _owns_instance_fields(function):
            class_info = classes[owner]
            for item in _walk_function(function):
                if (
                    isinstance(item, ast.AnnAssign)
                    and isinstance(item.target, ast.Attribute)
                    and isinstance(item.target.value, ast.Name)
                    and item.target.value.id == "self"
                    and _is_direct_optional_reference(
                        item.annotation,
                        bindings=body_bindings,
                    )
                ):
                    class_info.fields.add(item.target.attr)
        for item in _walk_function(function):
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                register_function(
                    item,
                    signature_bindings=body_bindings,
                    inherited_body_bindings=body_bindings,
                    owner=None,
                )
            elif isinstance(item, ast.ClassDef):
                register_class(
                    item,
                    inherited_body_bindings=body_bindings,
                )

    def register_class(
        node: ast.ClassDef,
        *,
        inherited_body_bindings: dict[str, ReferenceKind],
    ) -> None:
        class_info = _ClassInfo()
        classes[id(node)] = class_info
        forced_shadows = cross_scope_shadows | _type_parameter_names(node)
        nested_body_bindings = {
            name: kind
            for name, kind in inherited_body_bindings.items()
            if name not in forced_shadows
        }
        class_bindings = _reference_bindings_for_scope(
            node.body,
            nested_body_bindings,
            forced_shadows=forced_shadows,
        )
        for item in _walk_scope(node.body):
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                if _is_direct_optional_reference(
                    item.annotation,
                    bindings=class_bindings,
                ):
                    class_info.fields.add(item.target.id)
            elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                register_function(
                    item,
                    signature_bindings=class_bindings,
                    inherited_body_bindings=nested_body_bindings,
                    owner=id(node),
                )
            elif isinstance(item, ast.ClassDef):
                register_class(
                    item,
                    inherited_body_bindings=nested_body_bindings,
                )

    for node in _walk_scope(tree.body):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            register_function(
                node,
                signature_bindings=module_bindings,
                inherited_body_bindings=module_bindings,
                owner=None,
            )
        elif isinstance(node, ast.ClassDef):
            register_class(
                node,
                inherited_body_bindings=module_bindings,
            )

    return _ModuleInfo(path, classes, functions)


def _arguments(function: FunctionNode) -> list[ast.arg]:
    arguments = [
        *function.args.posonlyargs,
        *function.args.args,
        *function.args.kwonlyargs,
    ]
    if function.args.vararg is not None:
        arguments.append(function.args.vararg)
    if function.args.kwarg is not None:
        arguments.append(function.args.kwarg)
    return arguments


def _function_declarations(function: _FunctionInfo) -> set[str]:
    declarations = {
        argument.arg
        for argument in _arguments(function.node)
        if argument.annotation is not None
        and _is_direct_optional_reference(
            argument.annotation,
            bindings=function.signature_bindings,
        )
    }
    for node in _walk_function(function.node):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and _is_direct_optional_reference(
                node.annotation,
                bindings=function.body_bindings,
            )
        ):
            declarations.add(node.target.id)
    return declarations


def _truthiness_atoms(node: ast.expr) -> Iterable[ast.expr]:
    if isinstance(node, ast.Name | ast.Attribute):
        yield node
    elif isinstance(node, ast.NamedExpr):
        yield from _truthiness_atoms(node.value)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        yield from _truthiness_atoms(node.operand)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _truthiness_atoms(value)


def _tested_expressions(function: FunctionNode) -> Iterable[ast.expr]:
    for node in _walk_function(function):
        if isinstance(node, ast.If | ast.While | ast.Assert | ast.IfExp):
            yield from _truthiness_atoms(node.test)
        elif isinstance(node, ast.match_case) and node.guard is not None:
            yield from _truthiness_atoms(node.guard)
        elif isinstance(node, ast.BoolOp):
            for value in node.values[:-1]:
                yield from _truthiness_atoms(value)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield from _truthiness_atoms(node.operand)


def _is_declared_reference(
    expression: ast.expr,
    declarations: set[str],
    owner: _ClassInfo | None,
) -> bool:
    if isinstance(expression, ast.Name):
        return expression.id in declarations
    return (
        isinstance(expression, ast.Attribute)
        and isinstance(expression.value, ast.Name)
        and expression.value.id == "self"
        and owner is not None
        and expression.attr in owner.fields
    )


def _find_tree_violations(module: _ModuleInfo) -> list[Violation]:
    violations: dict[tuple[int, int], Violation] = {}
    for function in module.functions.values():
        declarations = _function_declarations(function)
        owner = (
            module.classes.get(function.owner)
            if function.owner is not None and _owns_instance_fields(function.node)
            else None
        )
        for expression in _tested_expressions(function.node):
            if not _is_declared_reference(expression, declarations, owner):
                continue
            violation = Violation(
                path=module.path,
                line=expression.lineno,
                column=expression.col_offset + 1,
                expression=ast.unparse(expression),
            )
            violations[(violation.line, violation.column)] = violation
    return sorted(violations.values())


def find_violations(paths: Iterable[Path]) -> list[Violation]:
    files = sorted(
        {
            file
            for path in paths
            for file in (path.rglob("*.py") if path.is_dir() else [path])
            if file.suffix == ".py"
        }
    )
    violations: list[Violation] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(_find_tree_violations(_collect_module_info(path, tree)))
    return sorted(violations)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reject truthiness checks on directly declared optional references."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)
    violations = find_violations(args.paths)
    for violation in violations:
        print(violation.format())
    if violations:
        print(
            "Use an explicit `is None` or `is not None` check so falsy user objects are preserved.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
