# Vision UI reproduction

The [sandbox setup guide](../../README.md) covers source installation, model credentials, and the Unix-local isolation limits. These tutorials also import `rich`; use `uv run --with rich` in place of `uv run` in the commands below when the environment does not install it.

## Goal

Use the sandbox `view_image` tool to inspect a reference app screenshot, then reproduce the visible screen as a static HTML/CSS artifact.

The agent calls `view_image`, loads the `playwright` skill on demand, and writes the site under `output/site/`. Playwright has been opening the generated files directly, without a web server, and capturing screenshots for visual review. The host copies the site and review artifacts back to this example's `output/` directory.

## Setup

### Unix-local

The Playwright skill has been requiring Node.js and npm, including `npx`, on the host's `PATH`. Its commands have been downloading Playwright 1.50.0 and Chromium on first use. On Linux, Chromium has also been requiring its system libraries; `npx --yes --package playwright@1.50.0 playwright install --with-deps chromium` has been installing them and the browser, with administrator privileges for system packages.

Run the Unix-local example from the repository root after preparing those dependencies:

```bash
uv run python examples/classic/sandbox/tutorials/vision_website_clone/main.py
```

### Docker

The shared tutorial image has been supplying Python tools without Node.js or Chromium's system libraries. The following commands have been building a derived image and selecting it through `--image`. Debian's `chromium` package has been supplying browser system libraries; Playwright has been installing its own pinned browser separately for the skill. Image preparation has been requiring a running Docker daemon and network access for package and browser downloads.

```bash
docker build -t sandbox-tutorials:latest -f examples/classic/sandbox/tutorials/Dockerfile .
docker build -t sandbox-tutorials-vision:latest -f - . <<'DOCKERFILE'
FROM sandbox-tutorials:latest
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm chromium \
    && npx --yes --package playwright@1.50.0 playwright install chromium \
    && rm -rf /var/lib/apt/lists/*
DOCKERFILE
uv run python examples/classic/sandbox/tutorials/vision_website_clone/main.py \
  --docker --image sandbox-tutorials-vision:latest
```

<a id="expected-artifact"></a>

## Expected artifacts

By default, the wrapper copies these files to the host directory `examples/classic/sandbox/tutorials/vision_website_clone/output/`:

- `index.html`
- `styles.css`
- `screenshots/draft-1.png`
- `screenshots/draft-2.png`
- `visual-notes.md`

Open `examples/classic/sandbox/tutorials/vision_website_clone/output/index.html` locally after the run to inspect the generated clone. Open the copied draft screenshots to inspect the agent's visual-debug loop.

## Demo shape

- Inputs: one checked-in PNG reference screenshot mounted under `reference/`.
- Runtime primitives: sandbox-local shell/edit tools, `view_image`, and the lazy-loaded `playwright` skill.
- Required vision call: `view_image("reference/reference-site.png")`.
- Required debug loop: capture `output/screenshots/draft-1.png`, view it with `view_image`, revise, then repeat with `output/screenshots/draft-2.png`.
- Artifact path: the sandbox agent writes `output/site/`, `output/screenshots/`, and `output/visual-notes.md`; `main.py` copies the site files and review artifacts to this example's `output/`.
