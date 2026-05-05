from pathlib import Path

from agents.sandbox.session.tar_workspace import shell_tar_exclude_args


def test_shell_tar_exclude_args_skips_empty_and_dot_paths() -> None:
    assert shell_tar_exclude_args([Path(""), Path("."), Path("/")]) == []


def test_shell_tar_exclude_args_sorts_and_adds_plain_and_dot_prefixed_patterns() -> None:
    assert shell_tar_exclude_args(
        [
            Path("logs/events.jsonl"),
            Path("cache dir/file.txt"),
        ]
    ) == [
        "--exclude='cache dir/file.txt'",
        "--exclude='./cache dir/file.txt'",
        "--exclude=logs/events.jsonl",
        "--exclude=./logs/events.jsonl",
    ]


def test_shell_tar_exclude_args_normalizes_absolute_paths() -> None:
    assert shell_tar_exclude_args([Path("/tmp/workspace/cache")]) == [
        "--exclude=tmp/workspace/cache",
        "--exclude=./tmp/workspace/cache",
    ]
