# RunState compatibility corpus

The `minimal/` fixtures cover every schema version accepted by the current reader. The `features/` fixtures cover the schema-bearing behavior introduced in versions 1.2 through 1.15. `sources.json` records the source commit and provenance for every fixture.

Regenerate the feature corpus from the recorded historical source trees with:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple uv run python tests/fixtures/run_state/generate_corpus.py
```

The generator extracts each recorded commit with `git archive` and runs that commit's writer in a fresh locked environment. It does not import the current checkout.

Versions 1.7 and 1.8 are explicit exceptions. Release-boundary schema renumbering assigned duplicate-agent/sandbox state to 1.7 and prompt-cache state to 1.8 without any writer commit that emitted those final version numbers. Their fixtures are therefore marked `canonical_compatibility`: the recorded 1.9 writer produces the payload, and the generator changes only the schema label so the corresponding reader branch remains covered. They must not be represented as historical-writer output.

Ordinary tests never run the generator. They read the frozen payloads, compare a semantic projection across the upgrade, rewrite to the current schema, and verify that the rewritten form is idempotent.
