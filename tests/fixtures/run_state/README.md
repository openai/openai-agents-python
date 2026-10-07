# RunState compatibility corpus

The `minimal/` fixtures cover every schema version accepted by the current reader. The `features/` fixtures cover the schema-bearing behavior introduced in versions 1.2 through 1.19. The `resume/` fixture records an actual pending function-tool approval emitted by the v0.19.4 writer, and the `security/` fixture records that writer's credential-bearing sandbox state. `sources.json` records the source commit and provenance for every fixture.

Regenerate the feature corpus from the recorded historical source trees with:

```bash
UV_DEFAULT_INDEX=https://pypi.org/simple uv run python tests/fixtures/run_state/generate_corpus.py
```

The generator extracts each recorded commit with `git archive` and runs that commit's writer in a fresh locked environment. It does not import the current checkout.

Versions 1.7, 1.8, 1.16, 1.17, and 1.19 are explicit exceptions. Release-boundary schema renumbering assigned duplicate-agent/sandbox state to 1.7 and prompt-cache state to 1.8 without any writer commit that emitted those final version numbers. The 1.16 schema transition likewise has no retained writer commit, but the retained 1.15 writer produces the same minimal and per-call-override payloads. The 1.17 labels transition uses the labels-capable 1.16 writer and changes only the schema label. Version 1.18 is not an exception: v0.23.0 released it, so its `minimal/` and `features/` fixtures are emitted by that tag's writer as `historical_writer` output. The unreleased 1.19 held-pending-write fixtures use the recorded 1.19 writer, this pull request's own commit, to serialize a canonical scenario containing the current-response boundary; they do not represent a released historical checkpoint. That writer commit lives on the pull request branch, so it must be re-pinned to the squashed merge commit before the 1.19 fixtures can be regenerated from main. These fixtures are therefore marked `canonical_compatibility`: the recorded writer produces the payload, and the generator changes the schema label where needed so the corresponding reader branch remains covered. They must not be represented as historical-writer output.

Ordinary tests never run the generator. They read the frozen payloads, compare every durable field emitted by the historical writer across the upgrade, rewrite to the current schema, and verify that the rewritten form is idempotent. They also approve and reject the historical pending interruption through actual `Runner` resumes. The schema version itself is the only normalization for the ordinary corpus; fields added by newer writers may be absent from an older payload, but every field present in that payload must survive. The security fixture has one explicit migration normalization: persisted mount credentials and opaque driver options are removed and the trusted-rebind marker is added. All non-authority topology remains part of the comparison.
