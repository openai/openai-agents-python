# Search an existing vector store

Complete the [hosted-tool setup](../README.md) first: install the matching preview wheel, activate its Python environment, and run from the source archive's root. Supply `OPENAI_API_KEY` through your environment. These commands make live OpenAI requests and can incur charges.

## Run the search

Set `OPENAI_VECTOR_STORE_ID` to a vector store you can access. Upload and index its files before running this example. The declaration does not create a store or upload files. You own store access, retention, and deletion.

```sh
export OPENAI_VECTOR_STORE_ID=vs_your_existing_store
python -m examples.codex.hosted_tools.file_search.main
```

Set `HOSTED_TOOL_PROMPT` to ask about your documents. The script prints the answer, raw retrieved matches, and file citations. `include_search_results=True` requests retrieval results separately from citations. Matches may be absent; the model can also answer without a search. Filters and ranking options depend on the provider's schema and the store's attributes.

The example uses `gpt-5.2` with OpenAI Responses and API-key authentication. Other models and providers may not support this tool. See the shared [setup and ownership notes](../README.md) and the provider's [file search guide](https://developers.openai.com/api/docs/guides/tools-file-search).
