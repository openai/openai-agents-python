import stat

import pytest

from agents.sandbox.types import Group, Permissions, User


@pytest.mark.parametrize(
    ("file_type", "is_directory"),
    [
        pytest.param(stat.S_IFDIR, True, id="directory"),
        pytest.param(stat.S_IFREG, False, id="regular-file"),
        pytest.param(stat.S_IFLNK, False, id="symlink"),
        pytest.param(stat.S_IFSOCK, False, id="socket"),
        pytest.param(stat.S_IFBLK, False, id="block-device"),
        pytest.param(stat.S_IFCHR, False, id="character-device"),
        pytest.param(stat.S_IFIFO, False, id="fifo"),
        pytest.param(0, False, id="permission-bits-only"),
    ],
)
def test_permissions_from_mode_classifies_file_type(file_type: int, is_directory: bool) -> None:
    permissions = Permissions.from_mode(file_type | 0o754)

    assert permissions.directory is is_directory
    assert (permissions.owner, permissions.group, permissions.other) == (0o7, 0o5, 0o4)
    assert str(permissions) == ("d" if is_directory else "-") + "rwxr-xr--"


def test_permissions_is_hashable() -> None:
    # ``Permissions`` overrides ``__eq__``; without a matching ``__hash__`` Pydantic v2
    # would set ``__hash__ = None``, breaking sets and dict keys for what is otherwise
    # a value-like type. Sibling classes ``User`` and ``Group`` already define both.
    perms = Permissions.from_mode(0o755)
    other = Permissions.from_mode(0o755)
    different = Permissions.from_mode(0o644)

    assert hash(perms) == hash(other)
    assert hash(perms) != hash(different)
    assert {perms, other, different} == {perms, different}
    assert {perms: "value"}[other] == "value"


def test_user_and_group_remain_hashable() -> None:
    # Regression guard for the sibling classes whose hashability the Permissions fix
    # mirrors.
    assert hash(User(name="alice")) == hash(User(name="alice"))
    assert hash(Group(name="admin", users=[])) == hash(Group(name="admin", users=[]))
