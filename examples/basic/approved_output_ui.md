# Show only approved final output

Run `uv run python examples/basic/approved_output_ui.py` from this checkout. The offline example retries once, calls an SDK tool, and completes. The public `on_llm_start` hook counts two outer requests while the fixture observes three adapter invocations. Trace-derived attempt counts remain unknown when the adapter supplies no evidence. No span count claims network delivery or billing.

The UI drains the approved-output reader, withholds every preview prefix, and applies its disclosure predicate only to the complete final text after successful producer cleanup. Split secrets and rejected final answers never enter its display. It reports running, paused, failed, cancelled, rejected and completed states without exception text. Cancellation propagates after producer cleanup.

The trace receiver publishes fixed labels and local parent aliases. The tool-start hook identifies native tools, SDK function tools and functions that call an external MCP server. A span alone cannot identify the executor. Other tool owners and native transport attempts remain unknown. Its metadata-only tracing scope prevents nested runs from opting back into automatic content capture. Configure processors once at service startup; do not replace active process-wide processors. Custom logs, names, raw consumers and private history need separate policy.

Pass an application-owned `run_config` to use its provider, including CodexProvider. This offline example does not verify live/native providers or add native interception. Reused metadata-scope and business-tracing work comes from PR #6128 at `0c7d9693cbe670e28ccff4ad0ec73e2ca38b969f`.
