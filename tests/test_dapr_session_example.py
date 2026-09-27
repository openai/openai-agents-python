from __future__ import annotations

import asyncio
import os
import runpy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote, urlsplit

import pytest
import yaml

pytest.importorskip("dapr")

from examples.memory import dapr_session_example as example


@pytest.fixture
def docker(monkeypatch):
    if os.name != "posix":
        pytest.skip("Local provisioning requires POSIX file permissions")
    monkeypatch.setenv("POSTGRES_PASSWORD", "synthetic-unique-password")
    monkeypatch.setattr(example.shutil, "which", lambda name: "/synthetic/docker")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))

    def dispatch(args, **kwargs):
        if args[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="unix:///synthetic/docker.sock\n", stderr=""
            )
        return run.return_value

    run.side_effect = dispatch
    monkeypatch.setattr(example.subprocess, "run", run)
    return run


def test_non_posix_setup_is_rejected_before_any_side_effects(tmp_path, monkeypatch):
    docker = Mock()
    monkeypatch.setattr(example.subprocess, "run", docker)
    # Model Windows without changing os.name globally or pathlib's host implementation.
    monkeypatch.setattr(example, "os", SimpleNamespace(name="nt"))
    components = tmp_path / "components"
    with pytest.raises(SystemExit, match="requires POSIX owner-only file permissions"):
        example.setup_environment(str(components), overwrite=True)
    docker.assert_not_called()
    assert not components.exists()


def test_setup_uses_loopback_and_matching_private_credentials(
    tmp_path, docker, monkeypatch, capsys
):
    password = "synthetic 'quoted' \\ value\" # : / @ ? % 密碼 🔑 \u0085"
    monkeypatch.setenv("POSTGRES_PASSWORD", password)
    components = tmp_path / "components"

    example.setup_environment(str(components))

    assert docker.call_args_list[0].args[0] == [
        "docker",
        "context",
        "inspect",
        "--format",
        "{{.Endpoints.docker.Host}}",
    ]
    assert docker.call_args_list[1].args[0] == [
        "docker",
        "--host",
        "unix:///synthetic/docker.sock",
        "container",
        "ls",
        "--all",
        "--format",
        "{{.Names}}",
    ]
    redis, postgres = docker.call_args_list[2:]
    assert redis.args[0] == [
        "docker",
        "--host",
        "unix:///synthetic/docker.sock",
        "run",
        "-d",
        "--name",
        "dapr_redis",
        "-p",
        "127.0.0.1:6379:6379",
        "redis:7-alpine",
    ]
    assert postgres.args[0] == [
        "docker",
        "--host",
        "unix:///synthetic/docker.sock",
        "run",
        "-d",
        "--name",
        "dapr_postgres",
        "-p",
        "127.0.0.1:5432:5432",
        "-e",
        "POSTGRES_USER=postgres",
        "-e",
        "POSTGRES_PASSWORD",
        "-e",
        "POSTGRES_DB=dapr",
        "postgres:16-alpine",
    ]
    assert postgres.kwargs["env"]["POSTGRES_PASSWORD"] == password
    for call in docker.call_args_list:
        assert password not in repr(call.args)
        assert call.kwargs["capture_output"] is True
    assert all("POSTGRES_PASSWORD" not in call.kwargs["env"] for call in docker.call_args_list[:-1])

    pg = yaml.safe_load((components / "statestore-postgres.yaml").read_text())
    metadata = pg["spec"]["metadata"]
    assert len(metadata) == 1
    assert metadata[0]["name"] == "connectionString"
    connection = urlsplit(metadata[0]["value"])
    assert connection.scheme == "postgresql"
    assert connection.username == "postgres"
    assert unquote(connection.password) == password
    assert connection.hostname == "127.0.0.1"
    assert connection.port == 5432
    assert connection.path == "/dapr"
    assert connection.query == connection.fragment == ""
    for filename, name in [
        ("statestore-redis.yaml", "statestore-redis"),
        ("statestore.yaml", "statestore"),
    ]:
        component = yaml.safe_load((components / filename).read_text())
        assert component["metadata"]["name"] == name
        assert component["spec"]["metadata"][0]["value"] == "127.0.0.1:6379"
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in components.iterdir())
    output = capsys.readouterr()
    assert password not in output.out + output.err
    assert "Environment setup complete" in output.out


@pytest.mark.parametrize(
    "password", [None, "", "  ", "postgres", " POSTGRES ", "postgres\nunique-suffix", "synthetic\r"]
)
def test_invalid_password_has_no_side_effects(tmp_path, docker, monkeypatch, password):
    if password is None:
        monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("POSTGRES_PASSWORD", password)
    components = tmp_path / "components"
    with pytest.raises(SystemExit, match="Set POSTGRES_PASSWORD"):
        example.setup_environment(str(components))
    docker.assert_not_called()
    assert not components.exists()


@pytest.mark.parametrize("name", ["dapr_redis", "dapr_postgres"])
@pytest.mark.parametrize("overwrite", [False, True])
def test_existing_container_is_never_started_or_changed(tmp_path, docker, name, overwrite):
    docker.return_value.stdout = f"unrelated\n{name}\n"
    components = tmp_path / "components"
    with pytest.raises(SystemExit, match="migrate manually"):
        example.setup_environment(str(components), overwrite=overwrite)
    assert docker.call_count == 2
    assert list(components.iterdir()) == []


def test_mismatched_component_stops_before_any_resources_change(tmp_path, docker):
    old = tmp_path / "statestore-postgres.yaml"
    old.write_text("operator-managed component")
    with pytest.raises(SystemExit, match="--overwrite"):
        example.setup_environment(str(tmp_path))
    assert docker.call_count == 1  # Local context inspection only.
    assert list(tmp_path.iterdir()) == [old]
    assert old.read_text() == "operator-managed component"


def test_overwrite_is_explicit_and_preserves_unrelated_files(tmp_path, docker):
    old = tmp_path / "statestore-postgres.yaml"
    old.write_text("operator-managed component")
    other = tmp_path / "other.yaml"
    other.write_text("unrelated")
    example.setup_environment(str(tmp_path), overwrite=True)
    assert "synthetic-unique-password" in old.read_text()
    assert other.read_text() == "unrelated"
    if os.name != "nt":
        assert old.stat().st_mode & 0o777 == 0o600


def test_matching_components_can_be_reused_without_overwrite(tmp_path, docker):
    example.setup_environment(str(tmp_path))
    original = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    docker.reset_mock()
    example.setup_environment(str(tmp_path))
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == original
    assert docker.call_count == 4


def test_component_symlink_is_not_followed_even_with_overwrite(tmp_path, docker):
    outside = tmp_path / "outside"
    outside.write_text("unrelated")
    components = tmp_path / "components"
    components.mkdir()
    (components / "statestore-postgres.yaml").symlink_to(outside)
    with pytest.raises(SystemExit, match="regular file"):
        example.setup_environment(str(components), overwrite=True)
    assert docker.call_count == 1  # Local context inspection only.
    assert outside.read_text() == "unrelated"
    assert len(list(components.iterdir())) == 1


@pytest.mark.parametrize("suffix", ["", "nested/components", "../components"])
def test_symlinked_component_directory_or_ancestor_is_rejected(tmp_path, docker, suffix):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "statestore-postgres.yaml"
    sentinel.write_text("operator-managed component")
    link = tmp_path / "linked"
    link.symlink_to(outside, target_is_directory=True)
    components = link / suffix
    with pytest.raises(SystemExit, match="ancestors must not be symlinks"):
        example.setup_environment(str(components), overwrite=True)
    docker.assert_not_called()
    assert sentinel.read_text() == "operator-managed component"
    assert list(outside.iterdir()) == [sentinel]
    assert not (tmp_path / "components").exists()


@pytest.mark.parametrize("failure", ["missing", "unavailable"])
def test_docker_preflight_failure_does_not_write_files(tmp_path, docker, monkeypatch, failure):
    if failure == "missing":
        monkeypatch.setattr(example.shutil, "which", lambda name: None)
    else:
        docker.return_value.returncode = 1
    with pytest.raises(SystemExit, match="Docker"):
        example.setup_environment(str(tmp_path / "components"))
    components = tmp_path / "components"
    assert not components.exists() or not list(components.iterdir())
    assert docker.call_count == (0 if failure == "missing" else 2)


def test_docker_creation_failure_is_reported_without_secret_output(tmp_path, docker, capsys):
    normal_dispatch = docker.side_effect
    docker.side_effect = [
        subprocess.CompletedProcess([], 0, stdout="unix:///synthetic/docker.sock", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="", stderr=""),
        subprocess.CompletedProcess(
            [], 1, stdout="synthetic-unique-password", stderr="synthetic-unique-password"
        ),
    ]
    with pytest.raises(SystemExit, match="partially created") as error:
        example.setup_environment(str(tmp_path))
    assert docker.call_count == 3
    assert "synthetic-unique-password" not in str(error.value)
    captured = capsys.readouterr()
    assert "synthetic-unique-password" not in captured.out + captured.err
    assert "Environment setup complete" not in captured.out
    docker.side_effect = normal_dispatch
    example.setup_environment(str(tmp_path))
    assert "Environment setup complete" in capsys.readouterr().out


def test_instructions_use_the_same_setup_helper_and_never_print_password(docker, capsys):
    asyncio.run(example.setup_instructions())
    output = capsys.readouterr().out
    assert "POSTGRES_PASSWORD" in output
    assert "--setup-env --only-setup" in output
    assert "127.0.0.1" in output
    assert "synthetic-unique-password" not in output
    assert "docker run" not in output
    docker.assert_not_called()


def test_cli_setup_only_does_not_run_demos(tmp_path, docker, monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "dapr_session_example.py",
            "--setup-env",
            "--only-setup",
            "--components-dir",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(asyncio, "run", Mock(side_effect=AssertionError("Must not run demos")))
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(example.__file__)), run_name="__main__")
    assert error.value.code == 0
    assert docker.call_count == 4


def test_cli_help_describes_required_credential_without_effects(docker, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["dapr_session_example.py", "--help"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(Path(example.__file__)), run_name="__main__")
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "POSTGRES_PASSWORD" in output
    assert "loopback-only" in output
    assert "existing named containers" in " ".join(output.split())
    assert "POSIX" in output
    assert "no symlinks" in output
    docker.assert_not_called()


@pytest.mark.parametrize(
    "endpoint,selection",
    [
        ("tcp://remote.example:2376", "DOCKER_HOST"),
        ("ssh://remote.example", "DOCKER_CONTEXT"),
        ("tcp://127.0.0.1:2375", None),
    ],
)
def test_non_unix_docker_endpoint_is_rejected_before_writes(
    tmp_path, docker, monkeypatch, endpoint, selection
):
    if selection:
        monkeypatch.setenv(selection, "operator-selected-target")
    docker.side_effect = None
    docker.return_value.stdout = endpoint
    with pytest.raises(SystemExit, match="local Docker Unix socket"):
        example.setup_environment(str(tmp_path / "components"))
    assert docker.call_count == 1
    assert "POSTGRES_PASSWORD" not in docker.call_args.kwargs["env"]
    if selection:
        assert docker.call_args.kwargs["env"][selection] == "operator-selected-target"
    assert not list(tmp_path.iterdir())


def test_docker_context_inspection_failure_has_no_side_effects(tmp_path, docker, capsys):
    docker.side_effect = None
    docker.return_value = subprocess.CompletedProcess(
        [], 1, stdout="synthetic-unique-password", stderr="synthetic-unique-password"
    )
    with pytest.raises(SystemExit, match="inspect the Docker endpoint"):
        example.setup_environment(str(tmp_path / "components"))
    assert docker.call_count == 1
    assert not list(tmp_path.iterdir())
    output = capsys.readouterr()
    assert "synthetic-unique-password" not in output.out + output.err


def test_local_docker_endpoint_is_pinned_despite_selection_changes(tmp_path, docker, monkeypatch):
    monkeypatch.setenv("DOCKER_CONTEXT", "local-desktop")
    monkeypatch.setenv("DOCKER_HOST", "tcp://unused.example:2376")

    def dispatch(args, **kwargs):
        if args[1:3] == ["context", "inspect"]:
            assert kwargs["env"]["DOCKER_CONTEXT"] == "local-desktop"
            assert kwargs["env"]["DOCKER_HOST"] == "tcp://unused.example:2376"
            monkeypatch.setenv("DOCKER_CONTEXT", "remote-after-inspection")
            return subprocess.CompletedProcess(
                args, 0, stdout="unix:///synthetic/desktop.sock", stderr=""
            )
        assert args[:3] == ["docker", "--host", "unix:///synthetic/desktop.sock"]
        assert "DOCKER_CONTEXT" not in kwargs["env"]
        assert "DOCKER_HOST" not in kwargs["env"]
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    docker.side_effect = dispatch
    example.setup_environment(str(tmp_path))
    assert docker.call_count == 4


def test_concurrent_setup_cannot_replace_winning_credentials(tmp_path, docker, monkeypatch):
    creating_redis = Event()
    finish_creation = Event()
    created = []

    def dispatch(args, **kwargs):
        if args[1:3] == ["context", "inspect"]:
            return subprocess.CompletedProcess(
                args, 0, stdout="unix:///synthetic/docker.sock", stderr=""
            )
        if args[3:5] == ["container", "ls"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n".join(created), stderr="")
        name = args[args.index("--name") + 1]
        if name == "dapr_redis":
            creating_redis.set()
            assert finish_creation.wait(10)
        else:
            assert kwargs["env"]["POSTGRES_PASSWORD"] == "winning-password"
        created.append(name)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    docker.side_effect = dispatch
    monkeypatch.setenv("POSTGRES_PASSWORD", "winning-password")
    components = tmp_path / "components"
    with ThreadPoolExecutor(max_workers=1) as pool:
        winner = pool.submit(example.setup_environment, str(components))
        try:
            assert creating_redis.wait(10)
            original = (components / "statestore-postgres.yaml").read_bytes()
            monkeypatch.setenv("POSTGRES_PASSWORD", "losing-password")
            with pytest.raises(SystemExit, match="Another setup"):
                example.setup_environment(str(components), overwrite=True)
            assert (components / "statestore-postgres.yaml").read_bytes() == original
            assert created == []
        finally:
            finish_creation.set()
        winner.result(timeout=10)
    pg = yaml.safe_load((components / "statestore-postgres.yaml").read_text())
    assert unquote(urlsplit(pg["spec"]["metadata"][0]["value"]).password) == "winning-password"
    assert created == ["dapr_redis", "dapr_postgres"]
    # The lock has been released; a later retry reaches the existing-resource check.
    with pytest.raises(SystemExit, match="Existing dapr_redis"):
        example.setup_environment(str(components), overwrite=True)


def test_setup_lock_is_released_after_preflight_failure(tmp_path, docker):
    old = tmp_path / "statestore-postgres.yaml"
    old.write_text("operator-managed component")
    with pytest.raises(SystemExit, match="--overwrite"):
        example.setup_environment(str(tmp_path))
    example.setup_environment(str(tmp_path), overwrite=True)
    assert "synthetic-unique-password" in old.read_text()
