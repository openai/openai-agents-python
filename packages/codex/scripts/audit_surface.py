"""Audit source coverage and optionally verify the pinned upstream archive.

This checks names, conversions, and source identity. It does not prove runtime
behavior or that Python can replace native host services.
"""

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_integrity import verify_pristine_source, verify_rmcp_source

HOST_FIELDS = {
    "internal_parent": (
        "Not bound: opaque captured internal-parent authority, not a user-supplied parent ID."
    ),
    "inherited_environments": (
        "Not bound: captured native environment handles; "
        "use explicit environments for public selections."
    ),
    "thread_extension_init": (
        "Partial: tool_policy and Environment capability directories are bound; "
        "arbitrary typed extension data and registry contributors are not."
    ),
    "turn_extension_init": (
        "Not bound: arbitrary native typed extension data cannot be supplied as Python JSON."
    ),
}


def masked_rust(source):
    """Keep offsets and newlines while masking comments and string/char contents."""
    result = list(source)
    pattern = re.compile(r'//[^\n]*|/\*|(?:b?r)(#*)"|b?"|b?\'(?:\\.|[^\'\\\n])\'')
    position = 0
    while match := pattern.search(source, position):
        start, end = match.span()
        token = match.group()
        if token == "/*":
            depth = 1
            while depth:
                marker = re.search(r"/\*|\*/", source[end:])
                if marker is None:
                    raise ValueError("Unterminated Rust block comment")
                depth += 1 if marker.group() == "/*" else -1
                end += marker.end()
        elif match.group(1) is not None:
            terminator = '"' + match.group(1)
            closing = source.find(terminator, end)
            if closing < 0:
                raise ValueError("Unterminated raw Rust string")
            end = closing + len(terminator)
        elif token in {'"', 'b"'}:
            while end < len(source):
                character = source[end]
                end += 1
                if character == "\\":
                    end += 1
                elif character == '"':
                    break
            else:
                raise ValueError("Unterminated Rust string")
        result[start:end] = ["\n" if char == "\n" else " " for char in source[start:end]]
        position = end
    return "".join(result)


def block(source, declaration, opening="{"):
    masked = masked_rust(source)
    matches = list(re.finditer(declaration, masked))
    if len(matches) != 1:
        raise ValueError(f"Expected one declaration for {declaration!r}, found {len(matches)}")
    start = masked.index(opening, matches[0].end())
    closing = {"{": "}", "[": "]"}[opening]
    depth = 1
    for index in range(start + 1, len(masked)):
        depth += (masked[index] == opening) - (masked[index] == closing)
        if depth == 0:
            return source[start + 1 : index]
    raise ValueError(f"Unterminated {declaration!r}")


def depths(source):
    depth = 0
    result = []
    for character in masked_rust(source):
        result.append(depth)
        depth += (character in "{[(") - (character in "}])")
    return result


def members(source, kind, name):
    body = block(source, rf"\b{kind}\s+{re.escape(name)}\b")
    levels = depths(body)
    pattern = r"(?m)^\s*(?:pub(?:\([^)]*\))?\s+)?([A-Za-z_]\w*)\s*(?=[:({,])"
    names = [
        match[1] for match in re.finditer(pattern, masked_rust(body)) if levels[match.start(1)] == 0
    ]
    if not names or len(names) != len(set(names)):
        raise ValueError(f"Could not extract unique members of {name}")
    return set(names)


def snake_case(name):
    name = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower()


def dispatched_operations(source):
    names = set()
    levels = depths(source)
    for match in re.finditer(r'(?m)^\s*((?:"[a-z_]+"\s*(?:\|\s*)?)+)=>', source):
        if levels[match.start(1)] == 0:
            names.update(re.findall(r'"([a-z_]+)"', match[1]))
    return names


def audit_sources(root):
    upstream = root / ".upstream/codex/codex-rs"
    protocol = (upstream / "protocol/src/protocol.rs").read_text()
    turn_input = (upstream / "protocol/src/turn_input.rs").read_text()
    config = (root / "rust/config.rs").read_text()
    operations = (root / "rust/ops.rs").read_text()
    thread = (root / "rust/options.rs").read_text()
    library = (root / "rust/lib.rs").read_text()
    errors = []
    checks = {}

    def fields(label, source, native_name, bridge, bridge_name, *, extra=False):
        expected = members(source, "struct", native_name)
        actual = members(bridge, "struct", bridge_name)
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected) if not extra else []
        if missing or unexpected:
            errors.append({"surface": label, "missing": missing, "unexpected": unexpected})
        checks[label] = {
            "native_fields": len(expected),
            "mapped_fields": len(expected & actual),
        }
        return expected

    runtime = fields(
        "ConfigOverrides",
        (upstream / "core/src/config/mod.rs").read_text(),
        "ConfigOverrides",
        config,
        "RuntimeOptions",
        extra=True,
    )
    assignments = set(re.findall(r"\b(\w+)\s*:\s*options\.\1\b", masked_rust(config)))
    if runtime - assignments:
        errors.append(
            {
                "surface": "ConfigOverrides assignments",
                "missing": sorted(runtime - assignments),
            }
        )
    fields(
        "LoaderOverrides",
        (upstream / "config/src/state.rs").read_text(),
        "LoaderOverrides",
        config,
        "LoaderOptions",
    )
    fields(
        "ExecServerRuntimeOptions",
        (upstream / "exec-server/src/runtime_options.rs").read_text(),
        "ExecServerRuntimeOptions",
        config,
        "RuntimeOptions",
        extra=True,
    )
    fields(
        "ThreadSettingsOverrides",
        protocol,
        "ThreadSettingsOverrides",
        operations,
        "ThreadSettings",
    )
    fields("TurnStartOptions", turn_input, "TurnStartOptions", operations, "StartOptions")
    fields("TurnSettingsUpdate", protocol, "TurnSettingsUpdate", operations, "TurnSettings")

    native_thread = members(
        (upstream / "core/src/thread_manager.rs").read_text(),
        "struct",
        "StartThreadOptions",
    )
    bridge_thread = members(thread, "struct", "ThreadOptions")
    mapped_thread = native_thread & bridge_thread - set(HOST_FIELDS)
    mapped_thread.add("thread_instructions_provider")
    assigned_thread = set(re.findall(r"\boptions\.(\w+)\s*=", masked_rust(thread)))
    if missing := mapped_thread - assigned_thread - {"config"}:
        errors.append({"surface": "StartThreadOptions assignments", "missing": sorted(missing)})
    if native_thread != mapped_thread | set(HOST_FIELDS):
        errors.append(
            {
                "surface": "StartThreadOptions",
                "unclassified": sorted(native_thread - mapped_thread - set(HOST_FIELDS)),
                "stale_exceptions": sorted(set(HOST_FIELDS) - native_thread),
            }
        )
    checks["StartThreadOptions"] = {
        "native_fields": len(native_thread),
        "mapped_fields": sorted(mapped_thread),
        "host_limits": HOST_FIELDS,
    }

    native_operations = {snake_case(name) for name in members(protocol, "enum", "Op")}
    catalog = set(
        re.findall(r'"([a-z_]+)"', block(operations, r"\bconst\s+OPERATIONS\b[^=]*=\s*&", "["))
    )
    catalog.update(re.findall(r'\boperations\.push\("([a-z_]+)"\)', library))
    dispatch = set()
    for selector in ("operation", "no_args"):
        dispatch_body = block(operations, rf"\bmatch\s+{selector}\b")
        dispatch.update(dispatched_operations(dispatch_body))
    dispatch.update(re.findall(r'\boperation\s*==\s*"([a-z_]+)"', library))
    if native_operations - catalog or catalog != dispatch:
        errors.append(
            {
                "surface": "Op dispatch",
                "unlisted_native_operations": sorted(native_operations - catalog),
                "unlisted_dispatch": sorted(dispatch - catalog),
                "missing_dispatch": sorted(catalog - dispatch),
            }
        )
    checks["operations"] = {
        "native_variants": len(native_operations),
        "exported_names": len(catalog),
        "dispatch_names": len(dispatch),
        "native_mapping": sorted(native_operations),
    }

    schema_path = upstream / "core/config.schema.json"
    schema = json.loads(schema_path.read_text())
    schema_body = block(library, r"\bfn\s+config_schema\s*\(\s*\)")
    include = re.search(r'include_str!\("([^"]+)"\)', schema_body)
    schema_direct = (
        include is not None and (root / "rust" / include[1]).resolve() == schema_path.resolve()
    )
    if not schema_direct or "wrap_pyfunction!(config_schema, module)" not in library:
        errors.append(
            {
                "surface": "configuration schema",
                "error": "Native export does not include the pinned schema directly",
            }
        )
    checks["configuration_schema"] = {
        "top_level_properties": len(schema["properties"]),
        "direct_native_export": schema_direct,
    }
    return checks, errors, schema, catalog


def verify_archive(root, source, revision):
    verified = verify_pristine_source(root)
    commit = subprocess.check_output(["git", "-C", str(source), "cat-file", "commit", revision])
    if commit != base64.b64decode(verified["codex_commit"], validate=True):
        raise SystemExit("Source repository differs from the authenticated Codex base")
    return {
        "tracked_files": verified["tracked_files"],
        "verified": True,
        "errors": [],
        "base_tree": verified["base_tree"],
        "result_tree": verified["tree"],
        "patch_series": verified["patch_series"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        help="Git repository containing CODEX_REVISION, for full archive verification",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help="Also compare the installed native schema, operation catalog, and revision",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = (root / "CODEX_REVISION").read_text().strip()
    pristine = verify_pristine_source(root)
    rmcp = verify_rmcp_source(root)
    checks, errors, schema, catalog = audit_sources(root)
    report = {
        "revision": revision,
        "source_tree": pristine["tree"],
        "rmcp_source": {
            "identity": rmcp["source"],
            "base_tree": rmcp["base_tree"],
            "result_tree": rmcp["tree"],
            "patch_series": rmcp["patch_series"],
        },
        "scope": (
            "Source/signature coverage only; not runtime behavior or complete host substitutability"
        ),
        "checks": checks,
        "errors": errors,
    }
    if args.source:
        report["upstream_archive"] = verify_archive(root, args.source, revision)
    else:
        report["upstream_archive"] = {
            "verified": False,
            "reason": "Pass --source to verify every tracked blob against Git",
        }
    if args.native:
        from agents import _native

        report["compiled_native"] = {
            "schema_matches": json.loads(_native.config_schema()) == schema,
            "operations_match": set(_native.OPERATIONS) == catalog,
            "revision_matches": _native.CODEX_REVISION == revision,
        }
        if not all(report["compiled_native"].values()):
            errors.append(
                {
                    "surface": "compiled native exports",
                    "error": "Installed extension differs from source",
                }
            )
    print(json.dumps(report, indent=2))
    raise SystemExit(
        bool(errors) or bool(args.source and not report["upstream_archive"]["verified"])
    )


if __name__ == "__main__":
    main()
