import stat

from agents.sandbox.types import Group, Permissions, User


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


def test_permissions_from_mode_uses_posix_file_type_predicate() -> None:
    file_types = [
        stat.S_IFDIR,
        stat.S_IFREG,
        stat.S_IFSOCK,
        stat.S_IFBLK,
        stat.S_IFCHR,
        stat.S_IFIFO,
        stat.S_IFLNK,
    ]

    for file_type in file_types:
        mode = file_type | 0o750
        permissions = Permissions.from_mode(mode)

        assert permissions.directory is stat.S_ISDIR(mode)
        assert permissions.owner == 0o7
        assert permissions.group == 0o5
        assert permissions.other == 0o0


def test_user_and_group_remain_hashable() -> None:
    # Regression guard for the sibling classes whose hashability the Permissions fix
    # mirrors.
    assert hash(User(name="alice")) == hash(User(name="alice"))
    assert hash(Group(name="admin", users=[])) == hash(Group(name="admin", users=[]))
