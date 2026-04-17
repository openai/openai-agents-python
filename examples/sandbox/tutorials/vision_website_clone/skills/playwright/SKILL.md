---
name: "playwright"
description: "Use when the task requires capturing or automating a real browser from the terminal."
---

# Playwright

Use Playwright to capture the static site directly. Do not start a server for
this example.

```sh
mkdir -p output/screenshots output/playwright/.tmp
export TMPDIR="$PWD/output/playwright/.tmp"
export TEMP="$TMPDIR"
export TMP="$TMPDIR"
npx --yes --package playwright@1.50.0 playwright install chromium
npx --yes --package playwright@1.50.0 playwright screenshot \
  --browser=chromium \
  --viewport-size=2048,1152 \
  "file://$PWD/output/site/index.html" \
  output/screenshots/draft-1.png
```

Change the final path to `output/screenshots/draft-2.png` for the second pass.
