# Running the example suite

Example execution is owned by the repository runner and Make targets. Run the complete auto-mode workflow in the foreground with:

    make examples-run

Pass runner arguments through `EXAMPLES_ARGS`, for example:

    make examples-run EXAMPLES_ARGS="--filter basic"
    make examples-run EXAMPLES_ARGS="--include-server --include-audio"

Use `make examples-run-background` for a background run. The remaining lifecycle targets are `make examples-status`, `make examples-stop`, `make examples-logs`, and `make examples-tail`. Set `EXAMPLES_LOG` to select a specific file for `examples-tail`.

Every normal run writes a main log and per-example logs under `.tmp/examples-start-logs/`. Use `EXAMPLES_ARGS="--filter <substring>"` to run a focused subset again when needed.

The defaults preserve auto input and approvals, include interactive examples, and exclude server, audio, and external examples unless selected. `EXAMPLES_UV_EXTRAS` controls the optional dependency extras installed by `uv`; set it to an empty value to disable extras. `EXAMPLES_INCLUDE_INTERACTIVE`, `EXAMPLES_INCLUDE_SERVER`, `EXAMPLES_INCLUDE_AUDIO`, and `EXAMPLES_INCLUDE_EXTERNAL` provide environment-based inclusion overrides.

The repository skill `examples-run-analysis` is analysis-only. After a manual run completes, use it to inspect the main log, every relevant per-example log, and example source. The skill never starts, retries, stops, or controls the example process.
