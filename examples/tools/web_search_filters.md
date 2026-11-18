# Search within official documentation domains

The original example has been comparing two models through the default Responses provider, checking both service-returned sources and citation annotations. The explicit Codex variant has been opening a fixed official function-tools documentation page and explaining that page. Both modes have been restricting web access to `developers.openai.com`.

From the repository root, after the normal SDK and stock Codex setup:

```bash
uv run python -m examples.tools.web_search_filters
uv run python -m examples.tools.web_search_filters --codex
```

Codex mode has been using `CODEX_MODEL` when set and the normal stock CLI authentication and configuration. It has not been passing Responses-only `response_include` settings or forcing the hosted web tool with `tool_choice`. Instead, it has been requiring a completed web `open_page` receipt for the requested official URL and matching every final Markdown citation against completed page opens. A generated URL alone has not been satisfying that check. Any opened or cited URL outside the allowed documentation domain has been failing verification.

Stock Codex has not been exposing the same retrieved-sources list or citation annotations as the Responses API. The explicit variant has been proving domain-restricted page retrieval and matching citations; it has not been claiming those missing API fields. The original default task and checks have been remaining available without `--codex`.
