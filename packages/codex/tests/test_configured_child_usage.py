import pytest

from _agents_codex.usage import RunUsage, _merge_configured_child_restore_usage, _UsageCollector


def usage(requests, tokens, missing=0):
    return RunUsage(tokens, 0, 0, 0, 0, tokens, requests, missing)


def add(owner, tokens):
    owner.add(usage(1, tokens))


@pytest.mark.parametrize("included", [False, True])
def test_restored_prefix_before_open(included):
    historical = _merge_configured_child_restore_usage(
        usage(3 if included else 2, 19 if included else 10),
        usage(1, 9),
        usage(1, 9) if included else None,
    )
    parent = _UsageCollector(historical)
    assert (parent.snapshot().requests, parent.snapshot().total_tokens) == (3, 19)
    child = _UsageCollector(usage(1, 9))
    parent.attach_configured_child(child, usage(1, 9))
    assert parent.snapshot() == historical
    add(child, 5)
    parent.attach_configured_child(child)
    assert (parent.snapshot().requests, parent.snapshot().total_tokens) == (4, 24)
    assert (parent.snapshot().requests, parent.snapshot().total_tokens) == (4, 24)


def test_rollback():
    parent = _UsageCollector(usage(3, 19))
    old = _UsageCollector(usage(1, 9))
    parent.attach_configured_child(old, usage(1, 9))
    add(old, 4)
    reopened = _UsageCollector(usage(1, 9))
    parent.attach_configured_child(reopened, usage(1, 9))
    add(reopened, 5)
    assert (parent.snapshot().requests, parent.snapshot().total_tokens) == (5, 28)


def test_nested():
    root = _UsageCollector(usage(4, 24))
    child = _UsageCollector(usage(2, 14))
    grandchild = _UsageCollector(usage(1, 5))
    root.attach_configured_child(child, usage(2, 14))
    child.attach_configured_child(grandchild, usage(1, 5))
    add(grandchild, 3)
    assert (root.snapshot().requests, root.snapshot().total_tokens) == (5, 27)


def test_distinct_occurrences_and_exact_counters():
    root = _UsageCollector()
    a = _UsageCollector()
    b = _UsageCollector()
    root.attach_configured_child(a)
    root.attach_configured_child(b)
    add(a, 9)
    add(b, 9)
    assert (root.snapshot().requests, root.snapshot().total_tokens) == (2, 18)
    exact = 2**60
    assert (
        _merge_configured_child_restore_usage(
            usage(3, exact + 9), usage(1, 9), usage(1, 9)
        ).total_tokens
        == exact + 9
    )


def test_plain_links_and_overlap():
    root = _UsageCollector(usage(2, 10))
    child = _UsageCollector(usage(1, 9))
    root.attach(child)
    root.attach(child)
    assert (root.snapshot().requests, root.snapshot().total_tokens) == (3, 19)
    with pytest.raises(ValueError, match="plain usage link"):
        root.attach_configured_child(child)


def test_bad_proof():
    for parent, child, proof in [
        (usage(2, 10), usage(1, 9), usage(2, 9)),
        (usage(2, 3), usage(1, 9), usage(1, 9)),
        (usage(2, 10, 1), usage(1, 9, 1), usage(1, 9)),
    ]:
        with pytest.raises(ValueError):
            _merge_configured_child_restore_usage(parent, child, proof)
