# MCP / Agents 選配（最小原則）

- 預設：僅啟用必要 Agents 與唯讀工具
- 外部 CLI：
  - Codex：預設 `--full-auto`（允許改檔、禁網）
  - Gemini：預設 `--output-format json --approval-mode auto_edit --allowed-tools "read_many_files,glob" -e none -s`
- 高風險資源（雲/K8s/DB）：預設唯讀，需升權時再徵詢
