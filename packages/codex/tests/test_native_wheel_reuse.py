"""Pack variants with current Python and the exact compiled native artifact."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from zipfile import ZipFile, ZipInfo

import pytest

COMMIT = b"tree " + b"0" * 40 + b"\n\nsynthetic build fixture\n"
RMCP_COMMIT = b"tree " + b"1" * 40 + b"\n\nsynthetic RMCP fixture\n"
RMCP_REVISION = hashlib.sha1(
    b"commit " + str(len(RMCP_COMMIT)).encode() + b"\0" + RMCP_COMMIT
).hexdigest()
FACTORY_GENERATOR = "build_dataclass_factories.py"
FACTORY_ARCHIVE = (
    f"_dataclass_factories_{sys.version_info.major}{sys.version_info.minor}-fixture.zip"
)
BUILDS_FACTORIES = (
    sys.implementation.name == "cpython"
    and sys.version_info[:2] in {(3, 12), (3, 13)}
    and not sys.flags.optimize
    and not sys._xoptions
)


def generates_factories(argv):
    return argv[:3] == [sys.executable, "-P", "-B"] and Path(argv[3]).name == FACTORY_GENERATOR


def rmcp_qualification(native):
    from source_integrity import rmcp_patch_series, rmcp_source_identity

    return {
        "identity": rmcp_source_identity(native),
        "commit": base64.b64encode(RMCP_COMMIT).decode(),
        "patch_series": rmcp_patch_series(native),
    }


@pytest.fixture
def hook_project(monkeypatch, tmp_path):
    # Hatchling runs in uv's isolated build environment, not the SDK test venv.
    # Stub only its base class; all archive and staging work uses the real hook.
    class BuildHookInterface:
        def __init__(self, root, *args):
            self.root = root

    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["hatchling.builders.hooks.plugin.interface"].BuildHookInterface = BuildHookInterface
    monkeypatch.delenv("OPENAI_AGENTS_NATIVE_RECEIPT_DIR", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from hatch_build import NativeBuildHook

    source = tmp_path / "variant/src/agents"
    source.mkdir(parents=True)
    (source / "__init__.py").write_bytes(b"VERSION = 'variant'\n")
    (source / "new_module.py").write_text("new = True\n")
    (source / "session.py").write_text("native_session = 'current'\n")
    (source / "py.typed").write_text("")
    (source.parents[1] / "LICENSE").write_text("Synthetic SDK license\n")
    (source.parents[1] / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    (source.parents[1] / "uv.lock").write_text("version = 1\n")
    native = tmp_path / "variant/packages/codex"
    (native / ".upstream").mkdir(parents=True)
    revision = hashlib.sha1(b"commit " + str(len(COMMIT)).encode() + b"\0" + COMMIT).hexdigest()
    (native / "CODEX_REVISION").write_text(revision)
    (native / "patches").mkdir()
    (native / "patches/series.json").write_text(
        json.dumps({"patches": [], "result_tree": "0" * 40})
    )
    from source_integrity import patch_series

    (native / ".upstream/codex-source.json").write_text(
        json.dumps(
            {"commit": base64.b64encode(COMMIT).decode(), "patch_series": patch_series(native)}
        )
    )
    for name in (
        "Cargo.toml",
        "Cargo.lock",
        "rust/lib.rs",
        "rust/helper.rs",
        "rust/ingress.c",
        "scripts/hatch_build.py",
        "scripts/build.py",
        "scripts/prepare_codex.py",
        "scripts/prepare_rmcp.py",
        "scripts/native_env.py",
        "scripts/native_helpers.py",
        "scripts/source_integrity.py",
        "scripts/pgo_train.py",
        "tests/native_fixtures.py",
    ):
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture " + name)
    (native / "scripts" / FACTORY_GENERATOR).write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from zipfile import ZipFile\n"
        "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "output.mkdir()\n"
        f"with ZipFile(output / {FACTORY_ARCHIVE!r}, 'x') as archive:\n"
        "    archive.writestr('_dataclass_factories.py', 'FACTORIES = {}\\n')\n"
    )
    (native / "rmcp-patches").mkdir()
    (native / "rmcp-patches/series.json").write_text(
        json.dumps({"patches": [], "result_tree": "1" * 40})
    )
    (native / "Cargo.toml").write_text(
        '[dependencies]\nrmcp = { git = "https://github.com/modelcontextprotocol/rust-sdk", '
        f'rev = "{RMCP_REVISION}", version = "=3.3.0" }}\n'
        '[patch."https://github.com/modelcontextprotocol/rust-sdk"]\n'
        'rmcp = { path = ".upstream/rust-sdk/crates/rmcp" }\n'
        'rmcp-macros = { path = ".upstream/rust-sdk/crates/rmcp-macros" }\n'
    )
    (native.parents[1] / "pyproject.toml").write_text(
        '[project]\nname="fixture"\n[build-system]\nrequires=["maturin"]\n'
        '[tool.maturin]\nmodule-name="agents._native"\n'
    )
    hook = NativeBuildHook(str(tmp_path / "variant"), {}, None, None, "", "wheel")
    return hook, source


def make_wheel(path, *, native_root, native=True, qualified=True, missing=None, native_build=None):
    from hatch_build import _native_inputs
    from source_integrity import patch_series

    suffix = ".exe" if sys.platform == "win32" else ""
    extension = "_native.pyd" if sys.platform == "win32" else "_native.abi3.so"
    files = {
        "agents/__init__.py": "VERSION = 'old'\n",
        "agents/removed_module.py": "must_not_reappear = True\n",
        "agents/licenses/SDK_LICENSE": "SDK license",
        "agents/licenses/CODEX_LICENSE": "public upstream license",
        "agents/licenses/CODEX_NOTICE": "public upstream notice",
        "agents/licenses/RMCP_LICENSE": "pinned RMCP license",
        "openai_agents.libs/libdependency.so": "repaired native dependency",
        f"agents/bin/codex-sdk-runtime{suffix}": "runtime",
        f"agents/bin/codex-code-mode-host{suffix}": "code mode",
        f"agents/bin/apply_patch{suffix}": "patch helper",
        f"agents/bin/applypatch{suffix}": "patch alias",
        f"agents/bin/codex-linux-sandbox{suffix}": "linux sandbox",
        f"agents/bin/codex-execve-wrapper{suffix}": "execve wrapper",
    }
    if native:
        files[f"agents/{extension}"] = "compiled native module"
    if missing is not None:
        files.pop(missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        for name, contents in files.items():
            info = ZipInfo(name)
            info.external_attr = 0o100755 << 16
            archive.writestr(info, contents)
    if qualified:
        path.with_name(path.name + ".pristine.json").write_text(
            json.dumps(
                {
                    "format": "reviewed-codex-rmcp-v4",
                    "patch_series": patch_series(native_root),
                    "native_inputs": _native_inputs(native_root),
                    "native_build": native_build or {"profile": "release", "pgo": False},
                    "codex_commit": base64.b64encode(COMMIT).decode(),
                    "rmcp_source": rmcp_qualification(native_root),
                    "wheel_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        )
    return extension


@pytest.fixture
def sdk_wheel(tmp_path):
    from packaging.tags import sys_tags

    return tmp_path / "artifact" / f"openai_agents-0.23.1-{next(sys_tags())}.whl"


def test_standard_wheel_owns_native_assets_once_without_gitignore(
    monkeypatch, hook_project, sdk_wheel
):
    hook, source = hook_project
    project = Path(hook.root)
    repository = Path(__file__).resolve().parents[3]
    for name in ("pyproject.toml", "README.md", "LICENSE", "packages/codex/README.md"):
        shutil.copyfile(repository / name, project / name)
    for name in ("hatch_build.py", "native_helpers.py", "source_integrity.py"):
        shutil.copyfile(
            repository / "packages/codex/scripts" / name,
            project / "packages/codex/scripts" / name,
        )
    extension = make_wheel(sdk_wheel, native_root=project / "packages/codex")
    suffix = ".exe" if sys.platform == "win32" else ""
    helper = f"bin/codex-code-mode-host{suffix}"
    for name in (extension, helper, "licenses/SDK_LICENSE", "bin/obsolete-helper"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale local build")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    # An extracted sdist has no Git ignore rules. Exercise Hatch's real file
    # selection together with the production hook, without compiling a runtime.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from hatchling.build import build_wheel; build_wheel(sys.argv[1])",
            str(project / "dist"),
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    (built,) = (project / "dist").glob("*.whl")
    with ZipFile(built) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        assert "agents/bin/obsolete-helper" not in names
        assert archive.read(f"agents/{extension}") == b"compiled native module"
        assert archive.read(f"agents/{helper}") == b"code mode"
        assert archive.read("agents/licenses/SDK_LICENSE") == b"SDK license"
        assert archive.read("agents/licenses/RMCP_LICENSE") == b"pinned RMCP license"
        assert archive.read("agents/__init__.py") == b"VERSION = 'variant'\n"
        assert (f"_agents_codex/{FACTORY_ARCHIVE}" in names) is BUILDS_FACTORIES
        assert not any(name.endswith(".pyc") for name in names)
        (metadata_name,) = (name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = archive.read(metadata_name).decode()
        description = metadata.split("\n\n", 1)[1]
        assert description.strip() == (repository / "packages/codex/README.md").read_text().strip()
        assert (
            "github.com/openai/openai-agents-python/blob/feat/codex-harness/docs/sdk/reference.md"
            in description
        )


@pytest.mark.parametrize("version", ["standard", "editable"])
def test_reuse_never_compiles_and_preserves_current_python(
    monkeypatch, hook_project, sdk_wheel, version
):
    hook, source = hook_project
    extension = make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex")
    suffix = ".exe" if sys.platform == "win32" else ""
    (source / "bin").mkdir()
    (source / "bin" / ("applypatch" + suffix)).write_text("old editable build")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    real_run = subprocess.run

    def generate_only(argv, **kwargs):
        assert generates_factories(argv), "Reuse must not invoke a compiler or fetch Codex"
        return real_run(argv, **kwargs)

    monkeypatch.setattr("hatch_build.subprocess.run", generate_only)
    data = {"force_include": {}}
    hook.initialize(version, data)
    try:
        if version == "editable":
            packaged = source
            libs = source.parent / "openai_agents.libs"
        else:
            entries = {value: Path(key) for key, value in data["force_include"].items()}
            packaged = entries["agents"]
            libs = entries["openai_agents.libs"]
            assert (f"_agents_codex/{FACTORY_ARCHIVE}" in entries) is BUILDS_FACTORIES
        assert (source / "__init__.py").read_text() == "VERSION = 'variant'\n"
        assert (source / "new_module.py").is_file()
        assert not (source / "removed_module.py").exists()
        assert not (packaged / "removed_module.py").exists()
        if version == "standard":
            # Hatch packages the root Python files; this overlay owns only native assets.
            assert not list(packaged.rglob("*.py"))
        assert (packaged / extension).read_text() == "compiled native module"
        assert (packaged / "licenses/CODEX_LICENSE").read_text() == "public upstream license"
        assert (libs / "libdependency.so").is_file()
        expected = {"codex-sdk-runtime", "codex-code-mode-host"}
        if sys.platform == "linux":
            expected.add("codex-linux-sandbox")
        if sys.platform != "win32":
            expected.add("codex-execve-wrapper")
        assert {path.name for path in (packaged / "bin").iterdir()} == {
            name + suffix for name in expected
        }
        for helper in expected:
            binary = packaged / "bin" / (helper + suffix)
            assert binary.is_file() and os.access(binary, os.X_OK)
        assert data["pure_python"] is False
    finally:
        hook.finalize(version, data, str(sdk_wheel))


@pytest.mark.parametrize(
    "problem", ["missing", "relative", "wrong-package", "missing-native", "missing-rmcp-license"]
)
def test_invalid_reuse_fails_instead_of_compiling(monkeypatch, hook_project, sdk_wheel, problem):
    hook, _ = hook_project
    make_wheel(
        sdk_wheel,
        native_root=Path(hook.root) / "packages/codex",
        native=problem != "missing-native",
        missing="agents/licenses/RMCP_LICENSE" if problem == "missing-rmcp-license" else None,
    )
    if problem == "missing":
        sdk_wheel.unlink()
    elif problem == "relative":
        sdk_wheel = Path(sdk_wheel.name)
    elif problem == "wrong-package":
        different = sdk_wheel.with_name(sdk_wheel.name.replace("openai_agents", "other_package"))
        sdk_wheel.rename(different)
        sdk_wheel = different
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    monkeypatch.setattr(
        "hatch_build.subprocess.run",
        lambda *args, **kwargs: pytest.fail("Invalid input must never fall back to compilation"),
    )
    reason = (
        "missing required native files"
        if problem in {"missing-native", "missing-rmcp-license"}
        else "OPENAI_AGENTS_NATIVE_WHEEL"
    )
    with pytest.raises(ValueError, match=reason):
        hook.initialize("standard", {"force_include": {}})


@pytest.mark.parametrize("version", ["standard", "editable"])
def test_fresh_thin_wheel_preserves_current_sdk_python_and_assets(
    monkeypatch, hook_project, sdk_wheel, version
):
    hook, source = hook_project
    monkeypatch.delenv("OPENAI_AGENTS_NATIVE_WHEEL", raising=False)
    calls = []
    real_run = subprocess.run

    def run(command, **kwargs):
        calls.append(command)
        if generates_factories(command):
            return real_run(command, **kwargs)
        if command[1].endswith("build.py"):
            output = Path(command[command.index("--out") + 1])
            wheel = output / "openai_agents-0.24.0a4-cp310-abi3-linux_x86_64.whl"
            make_wheel(wheel, native_root=Path(hook.root) / "packages/codex")
            (output / "native-build.json").write_text(
                json.dumps({"profile": "release" if version == "standard" else "dev", "pgo": False})
            )

    monkeypatch.setattr("hatch_build.subprocess.run", run)
    verified = []
    from source_integrity import patch_series

    def verify(path):
        verified.append(path)
        return {
            "codex_commit": base64.b64encode(COMMIT).decode(),
            "patch_series": patch_series(path),
        }

    monkeypatch.setattr("hatch_build.verify_pristine_source", verify)
    monkeypatch.setattr(
        "hatch_build.verify_rmcp_source",
        lambda path: {
            "source": rmcp_qualification(path)["identity"],
            "commit": rmcp_qualification(path)["commit"],
            "patch_series": rmcp_qualification(path)["patch_series"],
        },
    )
    data = {"force_include": {}}
    hook.initialize(version, data)
    assert verified == [Path(hook.root) / "packages/codex"]
    try:
        assert calls[0][1].endswith("prepare_codex.py")
        assert calls[1][1].endswith("build.py")
        assert ("--release" in calls[1]) is (version != "editable")
        entries = {value: Path(key) for key, value in data["force_include"].items()}
        packaged = source if version == "editable" else entries["agents"]
        libs = (
            source.parent / "openai_agents.libs"
            if version == "editable"
            else entries["openai_agents.libs"]
        )
        assert (source / "__init__.py").read_text() == "VERSION = 'variant'\n"
        assert (source / "session.py").read_text() == "native_session = 'current'\n"
        assert (source / "new_module.py").is_file()
        assert (source / "py.typed").is_file()
        assert not (source / "removed_module.py").exists()
        assert not (packaged / "removed_module.py").exists()
        if version == "standard":
            # Hatch packages the root Python files; this overlay owns only native assets.
            assert not list(packaged.rglob("*.py"))
        assert (packaged / "licenses/SDK_LICENSE").read_text() == "SDK license"
        for name in ("LICENSE", "NOTICE"):
            assert (
                packaged / f"licenses/CODEX_{name}"
            ).read_text() == f"public upstream {name.lower()}"
        assert (libs / "libdependency.so").read_text() == "repaired native dependency"
        suffix = ".exe" if sys.platform == "win32" else ""
        helper = packaged / "bin" / ("codex-sdk-runtime" + suffix)
        assert helper.read_text() == "runtime" and os.access(helper, os.X_OK)
        assert data["tag"] == "cp310-abi3-linux_x86_64"
        assert data["pure_python"] is False
    finally:
        make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex", qualified=False)
        hook.finalize(version, data, str(sdk_wheel))
    receipt = sdk_wheel.with_name(sdk_wheel.name + ".pristine.json")
    assert receipt.exists() is (version != "editable")
    if version != "editable":
        record = json.loads(receipt.read_text())
        assert record["wheel_sha256"] == hashlib.sha256(sdk_wheel.read_bytes()).hexdigest()
        assert record["codex_commit"] == base64.b64encode(COMMIT).decode()


@pytest.mark.parametrize("limits", [(1, 1_000_000), (1_000_000, 1)])
def test_wheel_budget_rejects_excess_and_cleans_staging(
    monkeypatch, hook_project, sdk_wheel, limits
):
    hook, _ = hook_project
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    monkeypatch.setattr("hatch_build.WHEEL_BUDGETS", {r".+": limits})
    data = {"force_include": {}}
    hook.initialize("standard", data)
    staging = Path(hook.staging.name)
    with pytest.raises(ValueError, match="Wheel payload exceeds regression budget"):
        hook.finalize("standard", data, str(sdk_wheel))
    assert not staging.exists()


@pytest.mark.parametrize(
    "problem", ["missing", "mutated-wheel", "wrong-pin", "old-format", "instrumented"]
)
@pytest.mark.parametrize("version", ["standard", "editable"])
def test_unqualified_wheel_is_rejected_before_staging(
    monkeypatch, hook_project, sdk_wheel, problem, version
):
    hook, source = hook_project
    make_wheel(
        sdk_wheel,
        native_root=Path(hook.root) / "packages/codex",
        qualified=problem != "missing",
    )
    receipt = sdk_wheel.with_name(sdk_wheel.name + ".pristine.json")
    if problem == "mutated-wheel":
        with sdk_wheel.open("ab") as output:
            output.write(b"changed")
    elif problem == "wrong-pin":
        (Path(hook.root) / "packages/codex/CODEX_REVISION").write_text("f" * 40)
    elif problem == "old-format":
        record = json.loads(receipt.read_text())
        record["format"] = "patched-codex"
        receipt.write_text(json.dumps(record))
    elif problem == "instrumented":
        record = json.loads(receipt.read_text())
        record["native_build"]["profile"] = "instrumented"
        receipt.write_text(json.dumps(record))
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    with pytest.raises(ValueError, match="matching reviewed-build receipt"):
        hook.initialize(version, {"force_include": {}})
    assert not hasattr(hook, "staging")
    assert not list(source.glob("_native.*"))


@pytest.mark.parametrize("pgo", [False, True])
def test_repacking_rebinds_receipt_to_exact_final_wheel(monkeypatch, hook_project, sdk_wheel, pgo):
    from hatch_build import _wheel_qualification

    hook, _ = hook_project
    build = {"profile": "release", "pgo": pgo, "maturin": "maturin 1.16.0"}
    if pgo:
        build["training_python_sha256"] = "a" * 64
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex", native_build=build)
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    data = {"force_include": {}}
    hook.initialize("standard", data)
    final = sdk_wheel.parent / "repacked" / sdk_wheel.name
    make_wheel(final, native_root=Path(hook.root) / "packages/codex", qualified=False)
    with ZipFile(final, "a") as archive:
        archive.writestr("agents/current.py", "current SDK source")
    hook.finalize("standard", data, str(final))
    record = json.loads(final.with_name(final.name + ".pristine.json").read_text())
    assert record["wheel_sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert record["wheel_sha256"] != hashlib.sha256(sdk_wheel.read_bytes()).hexdigest()
    assert record["native_build"] == build
    assert (
        _wheel_qualification(Path(hook.root) / "packages/codex", final)["format"]
        == "reviewed-codex-rmcp-v4"
    )


@pytest.mark.parametrize("failed_initialize", [False, True])
def test_uninitialized_or_failed_hook_cannot_qualify_wheel(
    monkeypatch, hook_project, sdk_wheel, failed_initialize
):
    hook, _ = hook_project
    make_wheel(
        sdk_wheel,
        native_root=Path(hook.root) / "packages/codex",
        native=False,
        qualified=True,
    )
    if failed_initialize:
        monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
        with pytest.raises(ValueError, match="missing required native files"):
            hook.initialize("standard", {"force_include": {}})
    output = sdk_wheel.with_name("unqualified.whl")
    output.write_bytes(b"not a qualified output")
    with pytest.raises(ValueError, match="did not establish a qualified build"):
        hook.finalize("standard", {}, str(output))
    assert not output.with_name(output.name + ".pristine.json").exists()


def test_failed_fresh_source_check_cannot_qualify_wheel(monkeypatch, hook_project, sdk_wheel):
    hook, _ = hook_project
    monkeypatch.delenv("OPENAI_AGENTS_NATIVE_WHEEL", raising=False)

    def run(command, **kwargs):
        if command[1].endswith("build.py"):
            output = Path(command[command.index("--out") + 1])
            make_wheel(
                output / sdk_wheel.name,
                native_root=Path(hook.root) / "packages/codex",
                qualified=False,
            )

    def reject(_):
        raise SystemExit("Codex source is not pristine")

    monkeypatch.setattr("hatch_build.subprocess.run", run)
    monkeypatch.setattr("hatch_build.verify_pristine_source", reject)
    with pytest.raises(SystemExit, match="not pristine"):
        hook.initialize("standard", {"force_include": {}})
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex", qualified=False)
    with pytest.raises(ValueError, match="did not establish a qualified build"):
        hook.finalize("standard", {}, str(sdk_wheel))
    assert not sdk_wheel.with_name(sdk_wheel.name + ".pristine.json").exists()


@pytest.mark.parametrize("directed_receipt", [False, True])
def test_frontend_wheel_copy_requires_matching_transferred_receipt(
    monkeypatch, hook_project, sdk_wheel, directed_receipt
):
    from hatch_build import _wheel_qualification

    hook, _ = hook_project
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    data = {"force_include": {}}
    hook.initialize("standard", data)
    backend = sdk_wheel.parent / "backend" / sdk_wheel.name
    make_wheel(backend, native_root=Path(hook.root) / "packages/codex", qualified=False)
    destination = sdk_wheel.parent / "frontend-dist"
    destination.mkdir()
    if directed_receipt:
        monkeypatch.setenv("OPENAI_AGENTS_NATIVE_RECEIPT_DIR", str(destination))
    hook.finalize("standard", data, str(backend))
    copied = destination / backend.name
    shutil.copy2(backend, copied)
    native = Path(hook.root) / "packages/codex"
    if directed_receipt:
        assert _wheel_qualification(native, copied)["format"] == "reviewed-codex-rmcp-v4"
    else:
        with pytest.raises(ValueError, match="matching reviewed-build receipt"):
            _wheel_qualification(native, copied)


def test_receipt_output_directory_must_be_absolute(monkeypatch, hook_project, sdk_wheel):
    hook, _ = hook_project
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    data = {"force_include": {}}
    hook.initialize("standard", data)
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_RECEIPT_DIR", "relative")
    with pytest.raises(ValueError, match="must be absolute"):
        hook.finalize("standard", data, str(sdk_wheel))


@pytest.mark.parametrize(
    "changed",
    [
        "rust/lib.rs",
        "rust/helper.rs",
        "rust/ingress.c",
        "Cargo.toml",
        "Cargo.lock",
        "scripts/build.py",
        "scripts/native_env.py",
        "scripts/native_helpers.py",
        "scripts/prepare_codex.py",
        "scripts/prepare_rmcp.py",
        "rmcp-patches/series.json",
        "scripts/source_integrity.py",
        "scripts/hatch_build.py",
        "scripts/pgo_train.py",
        "tests/native_fixtures.py",
        "../../uv.lock",
        "add",
        "remove",
        "maturin",
        "build-system",
        "sdk-license",
        "packaging-exclusions",
        "new-build-module",
        "hatch",
        "uv",
        "pgo-dependencies",
    ],
)
def test_stale_native_inputs_reject_before_payload_staging(
    monkeypatch, hook_project, sdk_wheel, changed
):
    hook, source = hook_project
    native = Path(hook.root) / "packages/codex"
    make_wheel(sdk_wheel, native_root=native)
    if changed == "sdk-license":
        (Path(hook.root) / "LICENSE").write_text("Updated synthetic SDK license\n")
    elif changed == "packaging-exclusions":
        path = Path(hook.root) / ".gitignore"
        path.write_text(path.read_text() + "!packages/codex/scripts/argparse.pyc\n")
    elif changed == "new-build-module":
        (native / "scripts/argparse.py").write_text("# Additional build import\n")
    elif changed == "add":
        (native / "rust/new.rs").write_text("new native module")
    elif changed == "remove":
        (native / "rust/helper.rs").unlink()
    elif changed == "pgo-dependencies":
        path = Path(hook.root) / "pyproject.toml"
        path.write_text(path.read_text() + '[dependency-groups]\npgo=["pytest>=99"]\n')
    elif changed in {"hatch", "uv"}:
        path = Path(hook.root) / "pyproject.toml"
        setting = (
            '[tool.hatch.build.hooks.custom]\npath="other.py"\n'
            if changed == "hatch"
            else '[tool.uv]\nbuild-constraint-dependencies=["maturin>=99"]\n'
        )
        path.write_text(path.read_text() + setting)
    elif changed in {"maturin", "build-system"}:
        path = Path(hook.root) / "pyproject.toml"
        text = path.read_text()
        text = (
            text.replace('module-name="agents._native"', 'module-name="agents.changed"')
            if changed == "maturin"
            else text.replace('["maturin"]', '["maturin>=99"]')
        )
        path.write_text(text)
    else:
        path = native / changed
        path.write_text(path.read_text() + "\nchanged native source")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    monkeypatch.setattr(
        "hatch_build.subprocess.run", lambda *a, **k: pytest.fail("no compile fallback")
    )
    with pytest.raises(ValueError, match="fresh native build"):
        hook.initialize("standard", {"force_include": {}})
    assert not hasattr(hook, "staging")
    assert not list(source.glob("_native.*"))


def test_previous_receipt_without_native_identity_rejected(monkeypatch, hook_project, sdk_wheel):
    hook, _ = hook_project
    make_wheel(sdk_wheel, native_root=Path(hook.root) / "packages/codex")
    path = sdk_wheel.with_name(sdk_wheel.name + ".pristine.json")
    record = json.loads(path.read_text())
    record.pop("native_inputs")
    path.write_text(json.dumps(record))
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    with pytest.raises(ValueError, match="fresh native build"):
        hook.initialize("standard", {"force_include": {}})
    assert not hasattr(hook, "staging")


def test_python_only_repack_carries_original_native_identity(monkeypatch, hook_project, sdk_wheel):
    from hatch_build import _wheel_qualification

    hook, source = hook_project
    native = Path(hook.root) / "packages/codex"
    make_wheel(sdk_wheel, native_root=native)
    before = _wheel_qualification(native, sdk_wheel)
    (source / "session.py").write_text("changed = 'Python policy only'\n")
    project = Path(hook.root) / "pyproject.toml"
    project.write_text(project.read_text().replace('name="fixture"', 'name="new-description"'))
    (Path(hook.root) / "README.md").write_text("new documentation")
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    data = {"force_include": {}}
    hook.initialize("standard", data)
    output = sdk_wheel.parent / "new" / sdk_wheel.name
    make_wheel(output, native_root=native, qualified=False)
    with ZipFile(output, "a") as archive:
        archive.writestr("agents/new.py", "new Python")
    hook.finalize("standard", data, str(output))
    assert _wheel_qualification(native, output) == before


@pytest.mark.parametrize("change_at", ["prepare", "build", "finalize", "failed-build"])
def test_mutating_or_failed_build_cannot_mint_receipt(
    monkeypatch, hook_project, sdk_wheel, change_at
):
    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    monkeypatch.delenv("OPENAI_AGENTS_NATIVE_WHEEL", raising=False)
    from source_integrity import patch_series

    monkeypatch.setattr(
        "hatch_build.verify_pristine_source",
        lambda p: {
            "codex_commit": base64.b64encode(COMMIT).decode(),
            "patch_series": patch_series(p),
        },
    )

    monkeypatch.setattr(
        "hatch_build.verify_rmcp_source",
        lambda path: {
            "source": rmcp_qualification(path)["identity"],
            "commit": rmcp_qualification(path)["commit"],
            "patch_series": rmcp_qualification(path)["patch_series"],
        },
    )

    real_run = subprocess.run

    def run(argv, **kwargs):
        if generates_factories(argv):
            return real_run(argv, **kwargs)
        if argv[1].endswith("prepare_codex.py") and change_at == "prepare":
            (native / "rust/ingress.c").write_text("mutated during prepare")
        if argv[1].endswith("build.py"):
            if change_at == "failed-build":
                raise RuntimeError("failed compiler")
            output = Path(argv[argv.index("--out") + 1])
            make_wheel(
                output / sdk_wheel.name,
                native_root=native,
                qualified=False,
            )
            (output / "native-build.json").write_text(
                json.dumps({"profile": "release", "pgo": False})
            )
            if change_at == "build":
                (native / "rust/lib.rs").write_text("mutated during compilation")

    monkeypatch.setattr("hatch_build.subprocess.run", run)
    if change_at == "finalize":
        hook.initialize("standard", {"force_include": {}})
        (native / "rust/helper.rs").write_text("mutated before publication")
    else:
        with pytest.raises((ValueError, RuntimeError), match="differ|failed compiler"):
            hook.initialize("standard", {"force_include": {}})
    make_wheel(sdk_wheel, native_root=native, qualified=False)
    with pytest.raises(ValueError, match="differ|did not establish"):
        hook.finalize("standard", {}, str(sdk_wheel))
    assert not sdk_wheel.with_name(sdk_wheel.name + ".pristine.json").exists()


def test_source_free_reuse_binds_reviewed_patch_identity(monkeypatch, hook_project, sdk_wheel):
    from hatch_build import _wheel_qualification
    from source_integrity import patch_series

    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    patch = native / "patches/0001-reviewed.patch"
    patch.write_bytes(b"reviewed synthetic patch bytes")
    manifest = {
        "patches": [{"path": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}],
        "result_tree": "1" * 40,
    }
    (native / "patches/series.json").write_text(json.dumps(manifest))
    make_wheel(sdk_wheel, native_root=native)
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    real_run = subprocess.run

    def generate_only(argv, **kwargs):
        assert generates_factories(argv), "Reuse must not build native source"
        return real_run(argv, **kwargs)

    monkeypatch.setattr("hatch_build.subprocess.run", generate_only)
    hook.initialize("standard", {"force_include": {}})
    assert _wheel_qualification(native, sdk_wheel)["patch_series"] == patch_series(native)
    assert not (native / ".upstream/codex").exists()
    hook.staging.cleanup()


@pytest.mark.parametrize("mutation", ["bytes", "order", "tree", "receipt", "old_format", "symlink"])
def test_reuse_rejects_patch_identity_drift_before_staging(
    monkeypatch, hook_project, sdk_wheel, mutation
):
    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    manifest = {"patches": [], "result_tree": "1" * 40}
    for number in (1, 2):
        patch = native / "patches" / f"000{number}-reviewed.patch"
        patch.write_bytes(f"reviewed bytes {number}".encode())
        manifest["patches"].append(
            {"path": patch.name, "sha256": hashlib.sha256(patch.read_bytes()).hexdigest()}
        )
    path = native / "patches/series.json"
    path.write_text(json.dumps(manifest))
    make_wheel(sdk_wheel, native_root=native)
    if mutation == "bytes":
        (native / "patches/0001-reviewed.patch").write_text("unreviewed")
    elif mutation == "order":
        manifest["patches"].reverse()
        path.write_text(json.dumps(manifest))
    elif mutation == "tree":
        manifest["result_tree"] = "2" * 40
        path.write_text(json.dumps(manifest))
    elif mutation == "symlink":
        if os.name == "nt":
            pytest.skip("Symlink permissions are not guaranteed on Windows")
        patch = native / "patches/0001-reviewed.patch"
        contents = patch.read_bytes()
        patch.unlink()
        target = native / "outside.patch"
        target.write_bytes(contents)
        patch.symlink_to(target)
    else:
        receipt_path = sdk_wheel.with_name(sdk_wheel.name + ".pristine.json")
        receipt = json.loads(receipt_path.read_text())
        if mutation == "receipt":
            receipt["patch_series"]["result_tree"] = "2" * 40
        else:
            receipt["format"] = "pristine-codex-v2"
        receipt_path.write_text(json.dumps(receipt))
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    with pytest.raises(ValueError, match="fresh native build"):
        hook.initialize("standard", {"force_include": {}})
    assert not hasattr(hook, "staging")


def test_reuse_checks_existing_prepared_source(monkeypatch, hook_project, sdk_wheel):
    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    make_wheel(sdk_wheel, native_root=native)
    (native / ".upstream/codex").mkdir()
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))

    def reject(root):
        assert root == native
        raise SystemExit("modified prepared result")

    monkeypatch.setattr("hatch_build.verify_pristine_source", reject)
    with pytest.raises(SystemExit, match="modified prepared result"):
        hook.initialize("standard", {"force_include": {}})
    assert not hasattr(hook, "staging")


@pytest.mark.parametrize("version", ["standard", "editable"])
def test_fresh_build_qualifies_verified_legacy_source_without_migration(
    monkeypatch, hook_project, sdk_wheel, version
):
    from source_integrity import (
        object_id,
        patch_series,
        rmcp_patch_series,
        rmcp_source_identity,
        tree_id,
        verify_pristine_source,
    )

    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    for name in ("prepare_codex.py", "prepare_rmcp.py", "source_integrity.py"):
        shutil.copyfile(scripts / name, native / "scripts" / name)
    tree = object_id("tree", b"")
    commit = b"tree " + tree.encode() + b"\n\ncanonical legacy source fixture\n"
    encoded_commit = base64.b64encode(commit).decode()
    (native / "CODEX_REVISION").write_text(object_id("commit", commit))
    (native / "patches/series.json").write_text(json.dumps({"patches": [], "result_tree": tree}))
    (native / ".upstream/codex").mkdir()
    receipt_path = native / ".upstream/codex-source.json"
    receipt_bytes = json.dumps({"commit": encoded_commit, "files": {}}).encode()
    receipt_path.write_bytes(receipt_bytes)
    rmcp = native / ".upstream/rust-sdk"
    rmcp.mkdir()
    cargo = b'[workspace.package]\nversion = "3.3.0"\n'
    (rmcp / "Cargo.toml").write_bytes(cargo)
    rmcp_files = {"Cargo.toml": ["100644", object_id("blob", cargo)]}
    rmcp_tree = tree_id(rmcp_files)
    rmcp_commit = b"tree " + rmcp_tree.encode() + b"\n\ncanonical RMCP fixture\n"
    cargo_path = native / "Cargo.toml"
    cargo_path.write_text(
        cargo_path.read_text().replace(RMCP_REVISION, object_id("commit", rmcp_commit))
    )
    (native / "rmcp-patches/series.json").write_text(
        json.dumps({"patches": [], "result_tree": rmcp_tree})
    )
    (native / ".upstream/rust-sdk-source.json").write_text(
        json.dumps(
            {
                "source": rmcp_source_identity(native),
                "commit": base64.b64encode(rmcp_commit).decode(),
                "files": rmcp_files,
                "result_files": rmcp_files,
                "patch_series": rmcp_patch_series(native),
            }
        )
    )
    monkeypatch.delenv("OPENAI_AGENTS_NATIVE_WHEEL", raising=False)
    real_run = subprocess.run
    compiled = []

    def compile_only(argv, **kwargs):
        if Path(argv[1]).name == "build.py":
            compiled.append(argv)
            output = Path(argv[argv.index("--out") + 1]) / sdk_wheel.name
            make_wheel(output, native_root=native, qualified=False)
            (output.parent / "native-build.json").write_text(
                json.dumps({"profile": "release" if version == "standard" else "dev", "pgo": False})
            )
            return subprocess.CompletedProcess(argv, 0)
        return real_run(argv, **kwargs)

    monkeypatch.setattr("hatch_build.subprocess.run", compile_only)
    data = {"force_include": {}}
    hook.initialize(version, data)
    assert len(compiled) == 1
    assert ("--release" in compiled[0]) == (version == "standard")
    assert hook.qualification["codex_commit"] == encoded_commit
    assert hook.qualification["patch_series"] == patch_series(native)
    assert verify_pristine_source(native)["codex_commit"] == encoded_commit
    make_wheel(sdk_wheel, native_root=native, qualified=False)
    hook.finalize(version, data, str(sdk_wheel))
    assert receipt_path.read_bytes() == receipt_bytes
    if version == "standard":
        qualified = json.loads(sdk_wheel.with_name(sdk_wheel.name + ".pristine.json").read_text())
        assert qualified["format"] == "reviewed-codex-rmcp-v4"
        assert qualified["codex_commit"] == encoded_commit
        assert qualified["patch_series"] == patch_series(native)


@pytest.mark.parametrize(
    "mutation", ["missing", "identity", "commit", "patch", "codex-only-format"]
)
def test_wheel_requires_both_fixed_source_identities(
    monkeypatch, hook_project, sdk_wheel, mutation
):
    hook, _ = hook_project
    native = Path(hook.root) / "packages/codex"
    make_wheel(sdk_wheel, native_root=native)
    receipt = sdk_wheel.with_name(sdk_wheel.name + ".pristine.json")
    record = json.loads(receipt.read_text())
    if mutation == "missing":
        record.pop("rmcp_source")
    elif mutation == "identity":
        record["rmcp_source"]["identity"]["rev"] = "f" * 40
    elif mutation == "commit":
        record["rmcp_source"]["commit"] = record["codex_commit"]
    elif mutation == "patch":
        record["rmcp_source"]["patch_series"]["result_tree"] = "f" * 40
    else:
        record["format"] = "reviewed-codex-v3"
    receipt.write_text(json.dumps(record))
    monkeypatch.setenv("OPENAI_AGENTS_NATIVE_WHEEL", str(sdk_wheel))
    with pytest.raises(ValueError, match="matching reviewed-build receipt"):
        hook.initialize("standard", {"force_include": {}})
    assert not hasattr(hook, "staging")
