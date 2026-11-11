# Read sample files with an MCP server

Activate the SDK environment with the [Classic extra](../../../../CLASSIC.md#install-the-classic-extra). Install Node.js with npm so `npx` is available on `PATH`, and set your [OpenAI API key](../../../../docs/published.md#set-an-openai-api-key).

From the repository root, run the example.

```sh
python -m examples.classic.mcp.filesystem_example.main
```

The script uses `npx` to start the [filesystem MCP server](https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem) with the bundled `sample_files` directory. The first run may need to download the server from npm. The agent lists the files, reads the favorite-books list, and suggests a song from the preferences in the sample data.
