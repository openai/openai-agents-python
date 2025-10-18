# Claude Code 專案啟動（最小版）

此檔用於完成自動準備並提供最小建議：
- 保守設定（高風險工具需詢問，讀取/搜尋允許）
- 讀檔型 Skills（唯讀）
- 自動交接外部 CLI（Codex/Gemini）採預設安全旗標

## 自動化步驟（已完成）
- 建立 `.claude/skills/`（repo-architecture、python-env-tasks、test-coverage）
- 建立 `.claude/flags/auto-interop.json`（預設啟用自動交接，專案層可覆蓋）
- 建議將 `.claude/interop/artifacts`、`.claude/interop/context` 加入 `.gitignore`

## 建議
- Python 開發流程：
  - `make sync && make check`（格式、lint、mypy、測試）
- 外部 CLI：
  - Codex（改檔、禁網）：`codex --full-auto -C "$PWD" -m <MODEL> exec -- "$(cat ctx.md)"`
  - Gemini（JSON、唯讀編輯、沙箱）：`gemini -m <MODEL> --output-format json --approval-mode auto_edit --allowed-tools "read_many_files,glob" -e none -s -p "@ctx.md"`
