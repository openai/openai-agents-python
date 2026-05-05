from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..logger import logger
from .entries import BaseEntry, Dir, Mount
from .workspace_paths import coerce_posix_path, posix_path_as_path

MAX_MANIFEST_DESCRIPTION_CHARS = 5000
MANIFEST_DESCRIPTION_TRUNCATION_MARKER_TEMPLATE = "... (truncated {omitted_chars} chars)"


def _truncate_manifest_description(description: str, max_chars: int | None) -> str:
    if max_chars is None or len(description) <= max_chars:
        return description
    if max_chars <= 0:
        return ""

    omitted_chars = len(description) - max_chars
    while True:
        marker = (
            "\n"
            + MANIFEST_DESCRIPTION_TRUNCATION_MARKER_TEMPLATE.format(omitted_chars=omitted_chars)
            + "\n\nThe filesystem layout above was truncated. "
            "Use `ls` to explore specific directories before relying on omitted paths.\n"
        )
        keep_chars = max(0, max_chars - len(marker))
        actual_omitted_chars = len(description) - keep_chars
        if actual_omitted_chars == omitted_chars:
            break
        omitted_chars = actual_omitted_chars

    truncated = description[:keep_chars].rstrip() + marker
    if len(marker) >= max_chars:
        truncated = marker[:max_chars]
        logger.warning(
            f"Manifest description exceeded {max_chars} characters "
            f"and was truncated to {len(truncated)} characters."
        )
        return truncated
    if len(truncated) > max_chars:
        truncated = truncated[:max_chars]
    logger.warning(
        f"Manifest description exceeded {max_chars} characters "
        f"and was truncated to {len(truncated)} characters."
    )
    return truncated


def render_manifest_description(
    *,
    root: str,
    entries: dict[str | Path, BaseEntry],
    coerce_rel_path: Callable[[str | Path], Path],
    depth: int | None = 1,
    max_chars: int | None = MAX_MANIFEST_DESCRIPTION_CHARS,
) -> str:
    if depth is not None and depth <= 0:
        raise ValueError("depth must be a non-zero positive integer or None")
    if max_chars is not None and max_chars <= 0:
        raise ValueError("max_chars must be a non-zero positive integer or None")

    root = root.rstrip("/") or "/"
    root_path = posix_path_as_path(coerce_posix_path(root))

    def _mount_full_path(entry: str | Path, artifact: Mount) -> Path:
        if artifact.mount_path is not None:
            mount_path = coerce_posix_path(artifact.mount_path)
            return posix_path_as_path(
                mount_path
                if mount_path.is_absolute()
                else coerce_posix_path(root_path) / mount_path
            )
        return root_path / coerce_rel_path(entry)

    class _Node:
        def __init__(self) -> None:
            self.children: dict[str, _Node] = {}
            self.description: str | None = None
            self.is_dir: bool = False
            self.full_path: Path | None = None

    def _path_parts(path: Path) -> tuple[str, ...]:
        parts = [part for part in coerce_posix_path(path).parts if part not in {"", "."}]
        return tuple(parts)

    root_node = _Node()

    def _insert_path(
        path: Path,
        *,
        description: str | None,
        is_dir: bool,
        full_path: Path | None = None,
        max_depth: int | None = None,
    ) -> None:
        parts = _path_parts(path)
        if not parts:
            return
        node = root_node
        limit = len(parts) if max_depth is None else min(len(parts), max_depth)
        for index, part in enumerate(parts[:limit]):
            node = node.children.setdefault(part, _Node())
            if index < len(parts) - 1:
                node.is_dir = True
        if node.description is None and description is not None and limit == len(parts):
            node.description = description
        if full_path is not None and limit == len(parts):
            node.full_path = full_path
        if is_dir or limit < len(parts):
            node.is_dir = True

    def _insert_entry_tree(
        path: Path,
        artifact: BaseEntry,
        *,
        full_path: Path | None = None,
    ) -> None:
        stack: list[tuple[Path, BaseEntry, Path | None]] = [(path, artifact, full_path)]
        while stack:
            current_path, current_artifact, current_full_path = stack.pop()
            _insert_path(
                current_path,
                description=current_artifact.description,
                is_dir=current_artifact.permissions.directory,
                full_path=current_full_path,
                max_depth=depth,
            )
            if not isinstance(current_artifact, Dir):
                continue
            if depth is not None and len(_path_parts(current_path)) >= depth:
                continue

            for child_name, child_artifact in current_artifact.children.items():
                child_rel_path = coerce_rel_path(child_name)
                child_path = current_path / child_rel_path
                child_full_path = (
                    current_full_path / child_rel_path if current_full_path is not None else None
                )
                stack.append((child_path, child_artifact, child_full_path))

    for entry, artifact in entries.items():
        path = coerce_rel_path(entry)
        if path.is_absolute():
            path = path.relative_to(path.anchor)
        full_path = _mount_full_path(entry, artifact) if isinstance(artifact, Mount) else None
        _insert_entry_tree(path, artifact, full_path=full_path)

    def _collect(
        node: _Node,
        prefix: str,
        remaining: int | None,
        rel_parts: tuple[str, ...],
    ) -> list[tuple[str, str, str, str | None]]:
        lines: list[tuple[str, str, str, str | None]] = []
        stack: list[tuple[str, _Node, str, int | None, tuple[str, ...]]]
        stack = [("children", node, prefix, remaining, rel_parts)]
        while stack:
            action, current_node, current_prefix, current_remaining, current_rel_parts = stack.pop()
            if action == "line":
                child = current_node
                name = current_rel_parts[-1]
                child_is_dir = child.is_dir or bool(child.children)
                display_name = f"{name}/" if child_is_dir else name
                if child.full_path is not None:
                    full_path = child.full_path.as_posix()
                else:
                    full_path = (
                        coerce_posix_path(root_path)
                        / coerce_posix_path("/".join(current_rel_parts))
                    ).as_posix()
                lines.append((current_prefix, display_name, full_path, child.description))
                continue

            if current_remaining is not None and current_remaining <= 0:
                continue

            names = sorted(current_node.children)
            next_remaining = None if current_remaining is None else current_remaining - 1
            for index in range(len(names) - 1, -1, -1):
                name = names[index]
                child = current_node.children[name]
                is_last = index == len(names) - 1
                connector = "└── " if is_last else "├── "
                child_parts = current_rel_parts + (name,)
                if next_remaining is None or next_remaining > 0:
                    extension = "    " if is_last else "│   "
                    stack.append(
                        (
                            "children",
                            child,
                            current_prefix + extension,
                            next_remaining,
                            child_parts,
                        )
                    )
                stack.append(
                    ("line", child, current_prefix + connector, next_remaining, child_parts)
                )
        return lines

    lines: list[str] = [root]
    collected = _collect(root_node, "", depth, ())
    if collected:
        max_width = max(len(prefix + name) for prefix, name, _, _ in collected)
        for prefix, name, full_path_str, description in collected:
            spacer = " " * (max_width - len(prefix + name) + 2)
            if description:
                comment = f"# {full_path_str} — {description}"
            else:
                comment = f"# {full_path_str}"
            lines.append(f"{prefix}{name}{spacer}{comment}")

    description = "\n".join(lines) + "\n"
    return _truncate_manifest_description(description, max_chars)
