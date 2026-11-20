# Search within official documentation domains

The default example compares two models through the Responses provider. It checks the service's retrieved sources and citation annotations. With `--codex`, the example opens a fixed official function-tools page and explains that page. Both modes restrict web access to `developers.openai.com`.

Complete the [Codex provider setup](../../docs/models/codex.md), then run from the repository root:

```sh
uv run python -m examples.tools.web_search_filters
uv run python -m examples.tools.web_search_filters --codex
```

Codex mode uses your Codex login and configuration. Set `CODEX_MODEL` to select a model. The example requires a completed web `open_page` result for the requested official URL. Each Markdown citation in the answer must match a completed page open. A generated URL alone does not pass that check. An opened or cited URL outside the allowed domain fails verification.

Codex does not expose the same retrieved-source list or citation annotations as the Responses API. The Codex example verifies domain-restricted retrieval and matching citations; it does not reproduce those API fields. Omit `--codex` to run the original comparison with Responses source checks.
