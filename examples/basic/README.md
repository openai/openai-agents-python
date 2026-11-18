# Running the basic examples with Codex

These paths have been using the stock Codex CLI with no account beyond Codex access. Commands below run from the repository root after SDK setup. Omitting the new switches has been preserving the original examples and their original service requirements.

```sh
# Two questions in the same SDK session, without a hosted response ID.
uv run -m examples.basic.previous_response_id --local --codex --stream

# Read the first sentence of the introduction from the bundled PDF.
uv run -m examples.basic.local_file --codex

# Have an SDK tool return a local image instead of accessing a photo URL.
uv run -m examples.basic.image_tool_output --codex --file examples/basic/media/image_bison.jpg
```

PDF examples have been requiring Poppler's `pdftotext` on your PATH (`poppler-utils` on Debian/Ubuntu, `poppler` with Homebrew). They have been extracting words; they haven't been doing OCR or interpreting the page layout. With `--codex`, [remote_pdf.py](remote_pdf.py) and [remote_image.py](remote_image.py) have been reading their example URLs in the app. Adding `--file PATH` has been using local data instead, which works without access to the PDF or image host. The SDK hasn't been following URLs supplied by the model.

For the local computer and data examples, see [running code locally](../tools/local_code_execution.md) and [querying a SQLite database](../sandbox/extensions/daytona/usaspending_text2sql/README.md).
