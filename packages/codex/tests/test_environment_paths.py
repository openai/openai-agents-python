"""Executor paths keep their URI encoding without deprecated pathlib calls."""

from pathlib import Path

import pytest

from agents import Environment


@pytest.mark.filterwarnings("error::DeprecationWarning")
@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/workspace", "file:///workspace"),
        (
            "/work dir/%literal#part?query/café",
            "file:///work%20dir/%25literal%23part%3Fquery/caf%C3%A9",
        ),
        ("//server/share", "file:////server/share"),
        (
            r"C:\work dir\%literal#part?query\café",
            "file:///C:/work%20dir/%25literal%23part%3Fquery/caf%C3%A9",
        ),
        (r"\\server\share\work dir\café", "file://server/share/work%20dir/caf%C3%A9"),
        ("file:///workspace/already%20encoded", "file:///workspace/already%20encoded"),
    ],
)
def test_remote_cwd_uri_encoding(cwd: str, expected: str) -> None:
    environment = Environment(url="wss://executor.invalid", cwd=cwd)
    assert environment._to_native()["cwd"] == expected


@pytest.mark.filterwarnings("error::DeprecationWarning")
def test_local_cwd_and_roots_keep_uri_encoding(tmp_path: Path) -> None:
    cwd = tmp_path / "work dir" / "café"
    root = "/work dir/%literal#part?query"
    environment = Environment.local(cwd=cwd, writable_roots=[root])
    assert environment.url == cwd.as_uri()
    native = environment._to_native()
    assert native["cwd"] == cwd.as_uri()
    assert native["writable_roots"] == ["file:///work%20dir/%25literal%23part%3Fquery"]


@pytest.mark.filterwarnings("error::DeprecationWarning")
@pytest.mark.parametrize(
    ("roots", "expected"),
    [
        (
            ["/work dir/café", "file://localhost/work%20dir/caf%C3%A9"],
            ["file:///work%20dir/caf%C3%A9"],
        ),
        (["file:///C:/work%20dir/caf%C3%A9"], ["file:///C%3A/work%20dir/caf%C3%A9"]),
    ],
)
def test_remote_roots_normalize_existing_file_uris(roots: list[str], expected: list[str]) -> None:
    environment = Environment(
        url="wss://executor.invalid",
        cwd="/workspace",
        writable_roots=roots,
        capability_directories=[r"C:\capability dir\café"],
    )
    native = environment._to_native()
    assert native["writable_roots"] == expected
    assert native["capability_directories"] == ["file:///C:/capability%20dir/caf%C3%A9"]
