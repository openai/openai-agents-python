"""Native builds must work outside short checkout paths."""

import hashlib
import io
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest


@pytest.mark.parametrize("status", [502, 503, 504])
def test_native_download_retries_transient_http_before_atomic_publication(
    monkeypatch, tmp_path, status
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    destination = tmp_path / "artifact"
    destination.write_bytes(b"old incomplete artifact")
    content = b"verified artifact"
    calls, delays, bodies = [], [], []

    def open_url(url, *, timeout):
        assert all(body.closed for body in bodies)
        calls.append((url, timeout))
        assert destination.read_bytes() == b"old incomplete artifact"
        assert len(list(tmp_path.glob("*.part"))) == 1
        if len(calls) < 3:
            body = io.BytesIO(b"temporary error body")
            bodies.append(body)
            raise urllib.error.HTTPError(url, status, "temporary gateway failure", None, body)
        return io.BytesIO(content)

    monkeypatch.setattr(native_env.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(native_env.time, "sleep", delays.append)
    assert (
        native_env._download(
            "https://example.invalid/artifact",
            destination,
            hashlib.sha256(content).hexdigest(),
        )
        == destination
    )
    assert calls == [("https://example.invalid/artifact", 60)] * 3
    assert delays == [1, 2]
    assert destination.read_bytes() == content
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize("status", [403, 404, 429, 502, 503, 504])
def test_native_download_preserves_http_failure_and_existing_destination(
    monkeypatch, tmp_path, status
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    destination = tmp_path / "artifact"
    destination.write_bytes(b"existing")
    calls, delays, failures = [], [], []

    def open_url(url, *, timeout):
        assert all(failure.closed for failure in failures)
        calls.append((url, timeout))
        failure = urllib.error.HTTPError(url, status, "failed", None, io.BytesIO(b"error body"))
        failures.append(failure)
        raise failure

    monkeypatch.setattr(native_env.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(native_env.time, "sleep", delays.append)
    with pytest.raises(urllib.error.HTTPError) as caught:
        native_env._download("https://example.invalid/artifact", destination, "wrong checksum")
    assert caught.value is failures[-1]
    assert not caught.value.closed
    caught.value.close()
    assert len(calls) == (3 if status in {502, 503, 504} else 1)
    assert delays == ([1, 2] if status in {502, 503, 504} else [])
    assert destination.read_bytes() == b"existing"
    assert list(tmp_path.glob("*.part")) == []


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("checksum"),
        TimeoutError("timeout"),
        urllib.error.URLError("connection"),
    ],
)
def test_native_download_does_not_retry_integrity_or_non_http_errors(
    monkeypatch, tmp_path, failure
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    destination = tmp_path / "artifact"
    calls, delays = [], []

    def open_url(url, *, timeout):
        calls.append((url, timeout))
        if isinstance(failure, ValueError):
            return io.BytesIO(b"wrong artifact")
        raise failure

    monkeypatch.setattr(native_env.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(native_env.time, "sleep", delays.append)
    with pytest.raises(type(failure)):
        native_env._download(
            "https://example.invalid/artifact",
            destination,
            hashlib.sha256(b"expected").hexdigest(),
        )
    assert len(calls) == 1 and delays == []
    assert not destination.exists()
    assert list(tmp_path.glob("*.part")) == []


def test_native_download_discards_partial_attempt_and_reuses_verified_cache(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import native_env

    content = b"verified artifact"
    destination = tmp_path / "artifact"
    calls, delays = [], []

    class Partial(io.BytesIO):
        def read(self, size):
            if self.tell():
                raise urllib.error.HTTPError(
                    "https://example.invalid/artifact", 504, "gateway", None, None
                )
            return super().read(size)

    def open_url(url, *, timeout):
        calls.append((url, timeout))
        return Partial(b"partial bytes") if len(calls) == 1 else io.BytesIO(content)

    monkeypatch.setattr(native_env.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(native_env.time, "sleep", delays.append)
    checksum = hashlib.sha256(content).hexdigest()
    assert (
        native_env._download("https://example.invalid/artifact", destination, checksum)
        == destination
    )
    assert destination.read_bytes() == content and len(calls) == 2 and delays == [1]
    assert list(tmp_path.glob("*.part")) == []
    assert (
        native_env._download("https://example.invalid/artifact", destination, checksum)
        == destination
    )
    assert len(calls) == 2 and delays == [1]


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
