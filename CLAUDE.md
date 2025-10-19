# CLAUDE.md（專案指引）

## 專案摘要
- 名稱：openai-agents-python
- 類型：Python SDK / Library
- 目的：OpenAI Agents SDK: multi-agent orchestration with guardrails, sessions, MCP, tracing, and provider-agnostic LLM support

## 重要腳本 / 指令
- 建置：make build-docs
- 測試：make tests
- 格式/靜態檢查：make format && make lint && make mypy
- 文件：mkdocs build || make build-docs

## 目錄結構（節選）

.
├── AGENTS.md
├── CLAUDE.md
├── LICENSE
├── Makefile
├── README.md
├── docs
├── examples
├── mkdocs.yml
├── pyproject.toml
├── src
├── tests
├── uv.lock

./src
├── agents

./tests
├── README.md
├── __init__.py
├── conftest.py
├── extensions
├── fake_model.py
├── fastapi
├── mcp
├── model_settings
├── models
├── realtime
├── test_agent_as_tool.py
├── test_agent_clone_shallow_copy.py
├── test_agent_config.py
├── test_agent_hooks.py
├── test_agent_instructions_signature.py

./docs
├── agents.md
├── assets
├── config.md
├── context.md
├── examples.md
├── guardrails.md
├── handoffs.md
├── index.md
├── ja
├── ko
├── llms-full.txt
├── llms.txt
├── mcp.md
├── models
├── multi_agent.md

## 開發規範
- 分支策略：feature/* branches → PR to main; keep commits small and readable
- Commit 信息：指令語態、簡潔摘要；可使用 /commit 或 /commit-push-pr
- PR 規範：摘要 + 測試計畫；可用 /handoff-* 取得候選，再由 Claude 收斂

## 測試與檢查
- 先跑格式與靜態檢查再執行測試；/auto-dev 會自動連續完成

## 安全與權限
- 高風險工具需詢問；Write/Edit 最小化；AutoGuard 啟用
- 外部 CLI：Codex(--full-auto, 禁網)、Gemini(json+auto_edit+唯讀白名單+沙箱)

## 自動交接
- 預設啟用；如需只建議不執行，將 .claude/flags/auto-interop.json 設為 {"enabled": false}
