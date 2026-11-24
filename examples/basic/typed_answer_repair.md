# Repair typed answers within a fixed budget

Run `uv run python examples/basic/typed_answer_repair.py` and `uv run python examples/mcp/bounded_tool_output/main.py` from this checkout with development dependencies. Neither needs model credentials.

The semantic repair example parses a Pydantic Verdict and checks its evidence count in the existing final-output policy. Dynamic instructions read fixed feedback from application-owned context. A second invalid candidate raises a fixed RepairLimitError. Provisional candidates may remain in private model history; approve only the validated final answer for display. The helper accepts run_config for an application-owned provider, including CodexProvider. Offline checks do not prove live/native behavior.

Malformed JSON or incompatible types fail during SDK parsing before final_output_policy. The separate tool-free `repair_schema` helper demonstrates two fresh synthetic attempts, with no session and no rejected text in the next request. It catches the SDK parser error without displaying its text, then fails with a fixed message when exhausted. Do not generalize new-run retries to providers that may perform native side effects.

The bounded-output recipe reuses PR #6120 at `482dac681ab40a5d9f037f11b7bfa90f482862b7`. Originals stay in a per-owner ephemeral store; authorized reads retrieve them privately and close revokes every reference. The integration converts local source-read exceptions into a fixed unavailable result before SDK tracing/history. It preserves the recipe's limits for small results, remote MCP errors, images, metadata, unselected results and native/hosted execution. A configured summary must itself be safe to disclose.
