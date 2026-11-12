# Read and revise selected documents

Give an Agent access to specific decoded UTF-8 files by assigning each file an alias. The application chooses the trusted root, paths, and writable aliases. The model uses those aliases in tool calls.

## Run the example

Install the SDK preview wheel and use the source archive from the same release. Follow the [installation guide](../../docs/sdk/installation.md) and configure `OPENAI_API_KEY` through your usual credential setup. Live model requests can incur charges. From the checkout root, run:

```sh
python -m examples.codex.selected_documents
```

The [example](selected_documents.py) creates a temporary text file, exposes it as `notes`, and asks the Agent to revise its status. It prints the answer and the status line read back from disk, then removes the temporary directory.

## Select files and connect tools

Import [`SelectedDocuments`](document_workspace.py) and supply `root`, a `selected` mapping from aliases to paths, and optional `writable` aliases. Pass the helper and an open `NativeProvider` to `document_agent()` in the example. The caller owns the provider and closes it after its Sessions. The helper uses ordinary `function_tool` callbacks; it needs no executor or hooks.

| Tool | What it returns or changes |
| --- | --- |
| `read_document` | Up to 4,096 Unicode code points, a SHA-256 content revision, and `next_offset`. |
| `search_document` | Up to 16 matches of up to 128 code points each. Continue with the same revision and `next_offset` to find phrases across page boundaries. |
| `revise_document` | Replaces one unique occurrence in a writable document. Supply its current revision and up to one page each of old and new text. The helper saves and reopens the file before reporting success. |

Follow the returned offset and revision to continue a read or search. A changed revision requires a fresh read. Stale tool calls return a failed `ToolOutput` so the model can try again. Direct helper methods raise `RevisionConflict`. Missing or ambiguous replacement text fails the run without changing the file. Unexpected filesystem errors also fail the run.

## File and concurrency limits

Supply application-created working copies as regular decoded UTF-8 files up to 16 MiB each. The helper reads each selected file into application memory. Extract text from PDF, Word, or Excel files before selecting it. The helper preserves UTF-8 BOM bytes and newline characters when it edits text. Unknown aliases, disallowed writes, paths outside the selected root, and selected paths that become symlinks are rejected.

The application must control the root and its parent directories. This recipe does not protect against a hostile process changing those directories. The helper serializes its own file operations. If other components write the same files, hold a shared application lock around the complete helper call or Agent run. Every external writer must use that same lock. Keep it held through replacement and verification; the helper's private lock coordinates only its own operations.

Each edit writes a temporary file next to the selected file and checks the revision again. It then replaces the original file and reopens it to verify the result. Replacement preserves text and permission bits, but not ownership, access-control lists (ACLs), extended attributes, or security labels. Select only working copies whose security permits replacement under the parent directory's policy. Use an application editor that preserves those properties when editing files that require them.

Cancellation waits for an in-flight file operation to finish before releasing the helper's lock. An edit may finish even when its caller is cancelled. Read the current revision and saved file before retrying; cancellation does not roll back a saved edit.

The page and match limits bound text returned by each call. Previous results can remain in model context, and JSON adds overhead. This recipe does not enforce a global context budget or search unselected project files.
