"""Exact native generation totals coexist with internal and external requests."""

import pytest

from _agents_codex.usage import RunUsage, TokenUsage, _UsageCollector


def tokens(count):
    return TokenUsage(count, 0, 0, 0, 0, count)


def snapshot(owner, requests, count, missing=0, terminal=None):
    return {
        "owner_attempt_id": owner,
        "terminal_attempt_id": terminal,
        "usage": {
            "requests": requests,
            "requests_without_token_usage": missing,
            "token_usage": {
                name: getattr(tokens(count), name) for name in TokenUsage.__dataclass_fields__
            },
        },
    }


def test_native_generation_duplicate_completions_do_not_duplicate_any_usage_dimension():
    usage = _UsageCollector()
    raw = {name: getattr(tokens(7), name) for name in TokenUsage.__dataclass_fields__}
    # The event and fact readers can interleave in either order.
    usage.observe(raw, "attempt-1")
    usage.observe(None, "attempt-1")
    usage.replace_model_attempt(snapshot("ledger-1", 2, 7, missing=1))
    usage.replace_model_attempt(snapshot("ledger-1", 2, 7, missing=1))
    usage.observe(raw, "attempt-1")
    assert usage.snapshot() == RunUsage(7, 0, 0, 0, 0, 7, 2, 1)


def test_untagged_compaction_legacy_and_explicit_requests_remain_additive():
    usage = _UsageCollector()
    usage.replace_model_attempt(snapshot("ledger-1", 2, 7, missing=1))
    usage.observe({name: getattr(tokens(11), name) for name in TokenUsage.__dataclass_fields__})
    usage.observe(None)
    usage.report(tokens(3))
    usage.replace_model_attempt(snapshot("ledger-1", 2, 7, missing=1), final=True)
    assert usage.snapshot() == RunUsage(21, 0, 0, 0, 0, 21, 5, 2)


def test_final_native_snapshot_preserves_exact_large_counts_and_ignores_late_facts():
    usage = _UsageCollector()
    count = 2**53 + 1
    usage.replace_model_attempt(snapshot("ledger-1", 3, count, terminal="attempt-3"), final=True)
    usage.replace_model_attempt(snapshot("ledger-1", 1, 0, missing=1))
    assert usage.snapshot().input_tokens == count
    assert usage.snapshot().requests == 3
    assert usage.model_terminal_attempt_id == "attempt-3"


def test_restore_seeds_full_historical_offset_once_beside_a_fresh_native_owner():
    old = _UsageCollector()
    old.replace_model_attempt(snapshot("old-ledger", 2, 9))
    old.report(tokens(3))
    restored = _UsageCollector(old.snapshot())
    restored.replace_model_attempt(snapshot("new-ledger", 1, 5), final=True)
    restored.replace_model_attempt(snapshot("new-ledger", 1, 5), final=True)
    assert restored.snapshot() == RunUsage(17, 0, 0, 0, 0, 17, 4, 0)


def test_child_totals_require_attachment_to_the_exact_owner():
    root, child, unrelated = _UsageCollector(), _UsageCollector(), _UsageCollector()
    root.replace_model_attempt(snapshot("root-ledger", 1, 2))
    child.replace_model_attempt(snapshot("child-ledger", 3, 5, missing=1))
    assert root.snapshot().requests == 1
    root.attach(child)
    assert root.snapshot() == RunUsage(7, 0, 0, 0, 0, 7, 4, 1)
    assert unrelated.snapshot().requests == 0


@pytest.mark.parametrize(
    "bad", [snapshot(None, 1, 0), snapshot("ledger-1", True, 0), snapshot("ledger-1", 1, 0, 2)]
)
def test_invalid_native_counter_snapshots_do_not_mutate_accepted_usage(bad):
    usage = _UsageCollector()
    usage.replace_model_attempt(snapshot("ledger-1", 1, 7))
    before = usage.snapshot()
    with pytest.raises(ValueError):
        usage.replace_model_attempt(bad)
    assert usage.snapshot() == before


def test_shared_run_keeps_repair_and_held_predecessor_ledgers_separate():
    run = _UsageCollector()
    predecessor, repair = _UsageCollector(), _UsageCollector()
    run.attach(predecessor)
    predecessor.replace_model_attempt(snapshot("predecessor", 1, 3))
    # A repair/retained successor can start before the predecessor's final event.
    run.attach(repair)
    repair.replace_model_attempt(snapshot("repair", 2, 7, missing=1), final=True)
    predecessor.replace_model_attempt(snapshot("predecessor", 1, 5), final=True)
    run.attach(repair)
    run.observe({name: getattr(tokens(11), name) for name in TokenUsage.__dataclass_fields__})
    assert run.snapshot() == RunUsage(23, 0, 0, 0, 0, 23, 4, 1)


def test_receipt_managed_physical_totals_wait_for_exact_native_terminal():
    usage = _UsageCollector()
    usage.replace_model_attempt(snapshot("physical-owner", 1, 0, missing=1))
    usage.manage_provider_attempts()
    usage.replace_model_attempt(snapshot("physical-owner", 2, 5, missing=1))
    assert usage.snapshot().requests == 0
    usage.replace_model_attempt(snapshot("physical-owner", 1, 5), final=True)
    assert usage.snapshot() == RunUsage(5, 0, 0, 0, 0, 5, 1, 0)


def test_receipt_managed_zero_create_keeps_restore_offset_and_rejects_other_owner():
    usage = _UsageCollector(RunUsage(7, 0, 0, 0, 0, 7, 3, 0))
    usage.replace_model_attempt(snapshot("physical-owner", 1, 0, missing=1))
    usage.manage_provider_attempts()
    with pytest.raises(ValueError, match="owner changed"):
        usage.replace_model_attempt(snapshot("other-owner", 1, 5), final=True)
    usage.replace_model_attempt(snapshot(None, 0, 0), final=True)
    assert usage.snapshot() == RunUsage(7, 0, 0, 0, 0, 7, 3, 0)


def test_ordinary_final_counter_regression_remains_rejected():
    usage = _UsageCollector()
    usage.replace_model_attempt(snapshot("ordinary-owner", 2, 0, missing=2))
    with pytest.raises(ValueError, match="regressed"):
        usage.replace_model_attempt(snapshot("ordinary-owner", 1, 0, missing=1), final=True)
