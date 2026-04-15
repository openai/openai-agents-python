# Vision UI reproduction

## Goal

Use the sandbox `view_image` tool to inspect a reference app screenshot, then
reproduce the visible screen as a static HTML/CSS artifact. This is a narrow UI
repro target for vision and screenshot-debugging; it is not a web-app scaffold.

This demo is intentionally file-only: no FastAPI, no exposed port, and no local
browser server. The agent calls `view_image`, lazy-loads the `playwright` skill,
writes the site under `output/site/`, captures browser screenshots for visual
revision, and the host copies the generated site plus the visual-review
artifacts back to this example's `output/` directory.

## Setup

Run the Unix-local example from the repository root:

```bash
uv run python examples/sandbox/tutorials/vision_website_clone/main.py
```

To run the same manifest in Docker, build the shared tutorial image once and pass
`--docker`:

```bash
docker build -t sandbox-tutorials:latest -f examples/sandbox/tutorials/Dockerfile .
uv run python examples/sandbox/tutorials/vision_website_clone/main.py --docker
```

## Expected artifact

- `output/index.html`
- `output/styles.css`
- `output/screenshots/draft-1.png`
- `output/screenshots/draft-2.png`
- `output/visual-notes.md`

Open `output/index.html` locally after the run to inspect the generated clone.
Open the copied draft screenshots to inspect the agent's visual-debug loop.

## Demo shape

- Inputs: one checked-in PNG reference screenshot mounted under `reference/`.
- Runtime primitives: sandbox-local shell/edit tools, `view_image`, and the
  lazy-loaded `playwright` skill.
- Required vision call: `view_image("reference/reference-site.png")`.
- Required debug loop: capture `output/screenshots/draft-1.png`, view it with
  `view_image`, revise, then repeat with `output/screenshots/draft-2.png`.
- Artifact path: the sandbox agent writes `output/site/`, `output/screenshots/`,
  and `output/visual-notes.md`; `main.py` copies the site files and review
  artifacts to this example's `output/`.
