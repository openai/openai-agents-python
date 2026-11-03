"""Native builds must work outside short checkout paths."""

import hashlib
import io
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("platform", "override"),
    [
        ("win32", None),
        ("win32", "custom-cargo"),
        ("linux", None),
        ("linux", "custom-cargo"),
    ],
)
def test_native_build_cargo_home_does_not_depend_on_windows_source_depth(
    monkeypatch, tmp_path, platform, override
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("RUSTY_V8_ARCHIVE", "preconfigured-v8-archive")
    monkeypatch.setenv("RUSTY_V8_SRC_BINDING_PATH", "preconfigured-v8-binding")
    monkeypatch.delenv("CARGO_HOME", raising=False)
    if override is not None:
        monkeypatch.setenv("CARGO_HOME", str(tmp_path / override))
    source = tmp_path / ("long-source-directory-" * 5) / "packages" / "codex"
    source.mkdir(parents=True)
    environment = native_env.build_environment(source)
    if override is not None:
        assert environment["CARGO_HOME"] == str(tmp_path / override)
    elif platform == "win32":
        # Cargo must choose its own per-user home, outside the pip source tree.
        assert "CARGO_HOME" not in environment
    else:
        assert environment["CARGO_HOME"] == str(source / ".cache/cargo")


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
def test_native_windows_build_supplies_checksum_verified_v8_binding(monkeypatch, tmp_path, arch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    for key in (
        "RUSTY_V8_ARCHIVE",
        "RUSTY_V8_SRC_BINDING_PATH",
        "RUSTY_V8_MIRROR",
        "V8_FORCE_DEBUG",
        "CARGO_BUILD_TARGET",
    ):
        monkeypatch.delenv(key, raising=False)
    target = f"{arch}-pc-windows-msvc"
    archive = f"rusty_v8_ptrcomp_sandbox_release_{target}.lib.gz"
    binding = f"src_binding_ptrcomp_sandbox_release_{target}.rs"
    manifest = f"rusty_v8_ptrcomp_sandbox_release_{target}.sha256"
    files = {archive: b"pinned V8 archive", binding: b"pinned V8 binding"}

    def line(name, content):
        return f"{hashlib.sha256(content).hexdigest()}  {name}\n"

    files[manifest] = (line(archive, files[archive]) + line(binding, files[binding])).encode()
    pins = tmp_path / ".upstream/codex/third_party/v8"
    pins.mkdir(parents=True)
    (tmp_path / "Cargo.lock").write_text('[[package]]\nname = "v8"\nversion = "150.4.0"\n')
    (pins / "rusty_v8_150_4_0.sha256").write_text(
        line(f"rusty_v8_release_{target}.lib.gz", files[archive])
    )
    (pins / "rusty_v8_150_4_0_release_manifests.sha256").write_text(line(manifest, files[manifest]))
    monkeypatch.setattr(
        native_env.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, f"host: {target}\n"),
    )
    monkeypatch.setattr(
        native_env.urllib.request,
        "urlopen",
        lambda url, **kwargs: io.BytesIO(files[url.rsplit("/", 1)[1]]),
    )

    environment = native_env.build_environment(tmp_path)

    assert Path(environment["RUSTY_V8_ARCHIVE"]).read_bytes() == files[archive]
    assert Path(environment["RUSTY_V8_SRC_BINDING_PATH"]).read_bytes() == files[binding]


@pytest.mark.parametrize(
    ("platform", "configured_target", "host", "timings", "develop"),
    [
        ("win32", None, "x86_64-pc-windows-msvc", False, False),
        ("win32", "aarch64-pc-windows-msvc", "aarch64-pc-windows-msvc", False, False),
        ("linux", None, "x86_64-unknown-linux-gnu", True, False),
        ("linux", None, "x86_64-unknown-linux-gnu", True, True),
        ("darwin", None, "aarch64-apple-darwin", False, False),
    ],
)
def test_helpers_and_extension_share_the_selected_target_and_crt(
    monkeypatch, tmp_path, platform, configured_target, host, timings, develop
):
    run_cli = subprocess.run
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import build

    root = tmp_path / "packages/codex"
    upstream = root / ".upstream/codex"
    upstream.mkdir(parents=True)
    (root / "CODEX_REVISION").write_text("pinned")
    (tmp_path / "LICENSE").write_text("SDK license\n")
    (upstream / "LICENSE").write_text("pinned upstream license\n")
    (upstream / "NOTICE").write_text("pinned upstream notice\n")
    rmcp = root / ".upstream/rust-sdk"
    rmcp.mkdir(parents=True)
    (rmcp / "LICENSE").write_text("pinned RMCP license\n")
    target_dir = tmp_path / "cargo-target"
    environment = {"CARGO_TARGET_DIR": str(target_dir)}
    if timings:
        environment["OPENAI_AGENTS_NATIVE_TIMINGS"] = "1"
    if configured_target:
        environment["CARGO_BUILD_TARGET"] = configured_target
    monkeypatch.setattr(build, "__file__", str(root / "scripts/build.py"))
    source_checks = []
    monkeypatch.setattr(
        build, "verify_pristine_source", lambda checked: source_checks.append(("codex", checked))
    )
    monkeypatch.setattr(
        build, "verify_rmcp_source", lambda checked: source_checks.append(("rmcp", checked))
    )
    monkeypatch.setattr(build, "build_environment", lambda _root: environment.copy())
    monkeypatch.setattr(sys, "platform", platform)
    mode_args = ["--develop"] if develop else ["--out", str(tmp_path / "dist")]
    monkeypatch.setattr(sys, "argv", ["build.py", "--release", *mode_args])
    extension_builds = []

    def run(command, **kwargs):
        if command == ["rustc", "-vV"]:
            assert configured_target is None
            return subprocess.CompletedProcess(command, 0, f"host: {host}\n")
        if command[0] == "cargo":
            assert ("--timings" in command) is timings
            assert "--release" in command and "--locked" in command
            if timings:
                result = run_cli([*command, "--help"], cwd=tmp_path, capture_output=True, text=True)
                assert result.returncode == 0, result.stderr
            if platform == "win32":
                assert command[command.index("--config") + 1] == str(
                    upstream / "codex-rs/.cargo/config.toml"
                )
                assert command[command.index("--target") + 1] == host
                destination = target_dir / host / "release"
            else:
                assert "--config" not in command and "--target" not in command
                destination = target_dir / "release"
            destination.mkdir(parents=True)
            suffix = ".exe" if platform == "win32" else ""
            for name in ("codex-sdk-runtime", "codex-code-mode-host"):
                (destination / f"{name}{suffix}").write_text(name)
        if command[0] == "maturin":
            licenses = tmp_path / "src/agents/licenses"
            assert {path.name: path.read_text() for path in licenses.iterdir()} == {
                "SDK_LICENSE": "SDK license\n",
                "CODEX_LICENSE": "pinned upstream license\n",
                "CODEX_NOTICE": "pinned upstream notice\n",
                "RMCP_LICENSE": "pinned RMCP license\n",
            }
            assert not any(arg.startswith("--timings") for arg in command)
            assert "--release" in command and "--locked" in command
            extension_builds.append(command)
            if platform == "win32":
                assert command[command.index("--config") + 1] == str(
                    upstream / "codex-rs/.cargo/config.toml"
                )
                assert command[command.index("--target") + 1] == host
            else:
                assert "--config" not in command and "--target" not in command
            assert kwargs["env"].get("CARGO_BUILD_TARGET") == configured_target
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build.subprocess, "run", run)
    suffix = ".exe" if platform == "win32" else ""
    helpers = tmp_path / "src/agents/bin"
    helpers.mkdir(parents=True)
    for alias in ("apply_patch", "applypatch", "codex-linux-sandbox", "codex-execve-wrapper"):
        (helpers / f"{alias}{suffix}").write_text("old build")
    build.main()
    assert source_checks == [("codex", root), ("rmcp", root)] * 3
    assert len(extension_builds) == 1
    if timings:
        result = run_cli(
            [*extension_builds[0], "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    expected = {
        "win32": {"codex-sdk-runtime", "codex-code-mode-host"},
        "darwin": {"codex-sdk-runtime", "codex-code-mode-host", "codex-execve-wrapper"},
        "linux": {
            "codex-sdk-runtime",
            "codex-code-mode-host",
            "codex-linux-sandbox",
            "codex-execve-wrapper",
        },
    }[platform]
    assert {path.name for path in helpers.iterdir()} == {name + suffix for name in expected}
    for name in expected:
        source = "codex-code-mode-host" if name == "codex-code-mode-host" else "codex-sdk-runtime"
        assert (helpers / f"{name}{suffix}").read_text() == source
