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


def test_user_and_group_remain_hashable() -> None:
    # Regression guard for the sibling classes whose hashability the Permissions fix
    # mirrors.
    assert hash(User(name="alice")) == hash(User(name="alice"))
    assert hash(Group(name="admin", users=[])) == hash(Group(name="admin", users=[]))
