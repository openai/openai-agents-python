# Dataroom Q&A

## Goal

Answer grounded financial questions over a synthetic 10-K packet.

The packet uses synthetic company data, but the documents are shaped like annual
report excerpts: MD&A text uses 10-K `Part II, Item 7`, while statement PDFs and
footnote text use `Part II, Item 8`.

## Why this is valuable

This demo shows a retrieval-first agent pattern over a bounded financial corpus
where each metric and explanation should stay tied to source files.

## Setup

Run the fixture generator and then the Unix-local example from the repository
root. Set `OPENAI_API_KEY` in your shell environment before running the example.

```bash
uv run python examples/sandbox/tutorials/data/dataroom/setup.py
uv run python examples/sandbox/tutorials/dataroom_qa/main.py
```

After the initial answer, the demo keeps the sandbox session open for
Rich-rendered follow-up prompts. Pass `--no-interactive` for a one-shot run.

To run the same manifest in Docker, build the shared tutorial image once and pass
`--docker`:

```bash
docker build --tag sandbox-tutorials:latest examples/sandbox/tutorials
uv run python examples/sandbox/tutorials/dataroom_qa/main.py --docker
```

## Expected artifacts

- A direct cited answer in the streamed agent response.
- Citations use `[n](data/source-file.txt:line:14)` for text excerpts and
  `[n](data/source-file.pdf:page:1)` for the one-page synthetic PDFs.

## Demo shape

- Inputs: 5 synthetic filing text docs and 3 simple filing PDFs from `examples/sandbox/tutorials/data/dataroom/`.
- Runtime primitives: sandbox-local bash/file search.

## How instructions are loaded

At startup, the wrapper loads this folder's `AGENTS.md` into the agent
instructions and builds a hard-coded manifest that maps the shared SEC packet
from `examples/sandbox/tutorials/data/dataroom/` into the sandbox as `data/...`.
