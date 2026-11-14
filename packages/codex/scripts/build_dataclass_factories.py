"""Stage interpreter-matched SDK dataclass factories without importing the native runtime."""

import argparse
import ast
import hashlib
import importlib
import py_compile
import sys
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo


def generate(project: Path, output: Path) -> Path:
    # Use this checkout, while retaining the frontend's isolated build dependencies.
    sys.path[:0] = [str(project / "src"), str(project / "packages/codex/src")]
    adapter = importlib.import_module("_agents_codex._compiled_dataclasses")
    fingerprint = adapter.driver_fingerprint()
    if fingerprint is None or sys.flags.optimize or sys._xoptions:
        raise ValueError("Dataclass factory generation requires an unmodified CPython 3.12/3.13")
    drivers = adapter.driver_functions()
    compiler = drivers.get("_create_fn", drivers.get("add_fns_to_class"))
    sources: dict[bytes, str] = {}

    def record(event, arguments):
        if event != "compile":
            return
        frame = sys._getframe(1)
        if frame.f_code is not compiler.__code__:
            return
        namespace = (
            frame.f_locals["globals"]
            if sys.version_info.minor == 12
            else frame.f_locals["self"].globals
        )
        if not namespace.get("__name__", "").startswith("_agents_codex."):
            return
        source = arguments[0]
        if isinstance(source, bytes):
            source = source.decode("utf-8")
        statements = ast.parse(source).body
        if (
            len(statements) != 1
            or not isinstance(statements[0], ast.FunctionDef)
            or statements[0].name != "__create_fn__"
        ):
            raise ValueError("The stock dataclass factory source has changed")
        sources[hashlib.sha256(source.encode()).digest()] = source

    # The hook lasts only for this build subprocess and never changes dataclasses.
    sys.addaudithook(record)
    public = importlib.import_module("agents")
    for name in ("Agent", "Session", "function_tool"):
        getattr(public, name)
    if "agents._native" in sys.modules:
        raise RuntimeError("Dataclass generation unexpectedly imported the native runtime")
    if not sources:
        raise RuntimeError("SDK imports did not produce dataclass factories")
    if adapter.driver_fingerprint() != fingerprint:
        raise RuntimeError("Dataclass functions changed during generation")

    output.mkdir()
    source_path = output / "_dataclass_factories.py"
    chunks = [
        '"""Wheel-generated SDK dataclass method factories."""\n',
        f"STDLIB_FINGERPRINT = {fingerprint!r}\n",
        "FACTORIES = {}\n",
    ]
    for key, source in sorted(sources.items()):
        chunks.extend((source, f"\nFACTORIES[{key!r}] = __create_fn__\n"))
    chunks.append("del __create_fn__\n")
    source_path.write_text("\n".join(chunks), encoding="utf-8", newline="\n")
    bytecode = output / "_dataclass_factories.pyc"
    py_compile.compile(
        str(source_path),
        cfile=str(bytecode),
        dfile="_agents_codex/_dataclass_factories.py",
        doraise=True,
        optimize=0,
        invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
    )
    archive = output / f"_dataclass_factories_3{sys.version_info.minor}-{fingerprint}.zip"
    with ZipFile(archive, "x", compression=ZIP_STORED) as bundle:
        for path in (source_path, bytecode):
            member = ZipInfo(path.name, date_time=(1980, 1, 1, 0, 0, 0))
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            bundle.writestr(member, path.read_bytes())
    print(f"Staged {len(sources)} SDK dataclass factories: {archive.name}")
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    options = parser.parse_args()
    generate(options.project.resolve(), options.output.resolve())


if __name__ == "__main__":
    main()
