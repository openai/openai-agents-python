"""
tests/test_cost_tracker.py

Run with:  pytest tests/test_cost_tracker.py -v
"""

import pytest
from agents.cost_tracker import CostTracker, _compute_cost


# ---------------------------------------------------------------------------
# Unit tests — pricing math
# ---------------------------------------------------------------------------
class TestPricing:
    def test_known_model(self):
        # gpt-4o-mini: $0.000150/1k input, $0.000600/1k output
        cost = _compute_cost("gpt-4o-mini", input_tokens=1000, output_tokens=1000)
        assert abs(cost - 0.00075) < 1e-9

    def test_prefix_matching(self):
        # "gpt-4o-mini-2024-07-18" should match "gpt-4o-mini"
        cost_versioned = _compute_cost("gpt-4o-mini-2024-07-18", 1000, 1000)
        cost_base      = _compute_cost("gpt-4o-mini", 1000, 1000)
        assert abs(cost_versioned - cost_base) < 1e-9

    def test_unknown_model_uses_fallback(self):
        cost = _compute_cost("some-future-model-9000", 1000, 0)
        assert cost > 0  # fallback kicks in, doesn't crash


# ---------------------------------------------------------------------------
# Unit tests — CostTracker accumulation
# ---------------------------------------------------------------------------
class TestCostTracker:
    def test_empty_tracker(self):
        t = CostTracker()
        assert t.total_cost() == 0.0
        assert t.total_tokens() == {"input": 0, "output": 0, "total": 0}

    def test_single_record(self):
        t = CostTracker()
        t.record(agent_name="Alice", model="gpt-4o-mini",
                 input_tokens=200, output_tokens=50)
        assert t.total_tokens()["input"] == 200
        assert t.total_tokens()["output"] == 50
        assert t.total_cost() > 0

    def test_multiple_agents_accumulate_separately(self):
        t = CostTracker()
        t.record(agent_name="Researcher", model="gpt-4o-mini",
                 input_tokens=300, output_tokens=80)
        t.record(agent_name="Writer", model="gpt-4o-mini",
                 input_tokens=100, output_tokens=40)

        assert t.by_agent["Researcher"].input_tokens == 300
        assert t.by_agent["Writer"].input_tokens == 100
        assert t.total_tokens()["input"] == 400

    def test_same_agent_multiple_calls(self):
        t = CostTracker()
        t.record(agent_name="Bot", model="gpt-4o-mini",
                 input_tokens=100, output_tokens=20)
        t.record(agent_name="Bot", model="gpt-4o-mini",
                 input_tokens=150, output_tokens=30)

        assert t.by_agent["Bot"].calls == 2
        assert t.by_agent["Bot"].input_tokens == 250

    def test_by_model_breakdown(self):
        t = CostTracker()
        t.record(agent_name="A", model="gpt-4o",      input_tokens=100, output_tokens=20)
        t.record(agent_name="B", model="gpt-4o-mini", input_tokens=200, output_tokens=40)

        assert "gpt-4o"      in t.by_model
        assert "gpt-4o-mini" in t.by_model
        assert t.by_model["gpt-4o"].input_tokens == 100

    def test_summary_structure(self):
        t = CostTracker()
        t.record(agent_name="X", model="gpt-4o-mini",
                 input_tokens=500, output_tokens=100)
        s = t.summary()

        assert "total_cost_usd"      in s
        assert "total_input_tokens"  in s
        assert "total_output_tokens" in s
        assert "total_calls"         in s
        assert "by_agent"            in s
        assert "by_model"            in s
        assert s["total_calls"] == 1

    def test_reset_clears_everything(self):
        t = CostTracker()
        t.record(agent_name="X", model="gpt-4o-mini",
                 input_tokens=100, output_tokens=20)
        t.reset()
        assert t.total_cost() == 0.0
        assert len(t.by_agent) == 0

    def test_thread_safety(self):
        """Hammer the tracker from many threads simultaneously."""
        import threading
        t = CostTracker()
        errors = []

        def worker():
            try:
                for _ in range(100):
                    t.record(agent_name="ThreadAgent", model="gpt-4o-mini",
                             input_tokens=10, output_tokens=5)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for th in threads: th.start()
        for th in threads: th.join()

        assert not errors
        assert t.by_agent["ThreadAgent"].calls == 2000

    def test_repr(self):
        t = CostTracker()
        t.record(agent_name="X", model="gpt-4o-mini",
                 input_tokens=100, output_tokens=20)
        r = repr(t)
        assert "CostTracker" in r
        assert "cost=$" in r