---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装を確認できます。これらのコード例は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   ストリーミングイベントを伴う Agents as tools (`examples/agent_patterns/agents_as_tools_streaming.py`)
    -   構造化入力パラメーターを伴う Agents as tools (`examples/agent_patterns/agents_as_tools_structured.py`)
    -   並列エージェント実行
    -   条件付きツール使用
    -   異なる挙動でツール使用を強制する (`examples/agent_patterns/forcing_tool_use.py`)
    -   入力 / 出力ガードレール
    -   審査者としての LLM
    -   ルーティング
    -   ストリーミングガードレール
    -   ツール承認と状態シリアライズを伴う Human-in-the-loop (`examples/agent_patterns/human_in_the_loop.py`)
    -   ストリーミングを伴う Human-in-the-loop (`examples/agent_patterns/human_in_the_loop_stream.py`)
    -   承認フロー向けのカスタム拒否メッセージ (`examples/agent_patterns/human_in_the_loop_custom_rejection.py`)

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基本機能を紹介します。

    -   Hello world のコード例 (デフォルトモデル、 GPT-5、 open-weight モデル)
    -   エージェントライフサイクル管理
    -   Run hooks と agent hooks のライフサイクル例 (`examples/basic/lifecycle_example.py`)
    -   動的システムプロンプト
    -   基本的なツール使用 (`examples/basic/tools.py`)
    -   ツール入力 / 出力ガードレール (`examples/basic/tool_guardrails.py`)
    -   画像ツール出力 (`examples/basic/image_tool_output.py`)
    -   ストリーミング出力 (テキスト、項目、関数呼び出し引数)
    -   複数ターンで共有セッションヘルパーを使用する Responses websocket transport (`examples/basic/stream_ws.py`)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   使用状況追跡
    -   Runner 管理の再試行設定 (`examples/basic/retry.py`)
    -   サードパーティアダプター経由の Runner 管理再試行 (`examples/basic/retry_litellm.py`)
    -   非 strict な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた、構造化された調査ワークフローを示す金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを含む、エージェントのハンドオフの実践的なコード例です。

    -   メッセージフィルター例 (`examples/handoffs/message_filter.py`)
    -   ストリーミングを伴うメッセージフィルター (`examples/handoffs/message_filter_streaming.py`)

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    OpenAI Responses API で hosted MCP (Model Context Protocol) を使用する方法を示すコード例です。以下を含みます。

    -   承認なしのシンプルな hosted MCP (`examples/hosted_mcp/simple.py`)
    -   Google Calendar などの MCP コネクター (`examples/hosted_mcp/connectors.py`)
    -   割り込みベース承認を伴う Human-in-the-loop (`examples/hosted_mcp/human_in_the_loop.py`)
    -   MCP ツール呼び出しの on-approval コールバック (`examples/hosted_mcp/on_approval.py`)

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    以下を含め、 MCP (Model Context Protocol) でエージェントを構築する方法を学べます。

    -   Filesystem のコード例
    -   Git のコード例
    -   MCP prompt server のコード例
    -   SSE (Server-Sent Events) のコード例
    -   SSE リモートサーバー接続 (`examples/mcp/sse_remote_example`)
    -   Streamable HTTP のコード例
    -   Streamable HTTP リモート接続 (`examples/mcp/streamable_http_remote_example`)
    -   Streamable HTTP 向けカスタム HTTP client factory (`examples/mcp/streamablehttp_custom_client_example`)
    -   `MCPUtil.get_all_function_tools` による全 MCP ツールの事前取得 (`examples/mcp/get_all_mcp_tools_example`)
    -   FastAPI を使用した MCPServerManager (`examples/mcp/manager_example`)
    -   MCP ツールフィルタリング (`examples/mcp/tool_filter_example`)

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例です。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr state store セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ
    -   `ModelSettings(store=False)` を使用したステートレスな Responses compaction (`examples/memory/compaction_session_stateless_example.py`)
    -   ファイルベースのセッションストレージ (`examples/memory/file_session.py`)
    -   Human-in-the-loop を伴うファイルベースセッション (`examples/memory/file_hitl_example.py`)
    -   Human-in-the-loop を伴う SQLite インメモリセッション (`examples/memory/memory_session_hitl_example.py`)
    -   Human-in-the-loop を伴う OpenAI Conversations セッション (`examples/memory/openai_session_hitl_example.py`)
    -   セッションをまたぐ HITL 承認 / 拒否シナリオ (`examples/memory/hitl_session_scenario.py`)

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーやサードパーティアダプターを含め、 SDK で非 OpenAI モデルを使用する方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使用してリアルタイム体験を構築する方法を示すコード例です。以下を含みます。

    -   構造化されたテキストおよび画像メッセージによる Web アプリケーションパターン
    -   コマンドライン音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API attach フローを使用した Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content の扱い方を示すコード例です。以下を含みます。

    -   Runner API、ストリーミング、非ストリーミングでの reasoning content (`examples/reasoning_content/runner_example.py`)
    -   OpenRouter 経由で OSS モデルを使用した reasoning content (`examples/reasoning_content/gpt_oss_stream.py`)
    -   基本的な reasoning content のコード例 (`examples/reasoning_content/main.py`)

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    以下のような OpenAI がホストするツールと実験的な Codex ツール機能の実装方法を学べます。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   ファイル編集と承認を伴う apply patch ツール (`examples/tools/apply_patch.py`)
    -   承認コールバックを伴う shell ツール実行 (`examples/tools/shell.py`)
    -   Human-in-the-loop 割り込みベース承認を伴う shell ツール (`examples/tools/shell_human_in_the_loop.py`)
    -   インラインスキルを伴う hosted container shell (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照を伴う hosted container shell (`examples/tools/container_shell_skill_reference.py`)
    -   ローカルスキルを伴う local shell (`examples/tools/local_shell_skill.py`)
    -   名前空間と遅延ツールを伴うツール検索 (`examples/tools/tool_search.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex 同一スレッドワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、 TTS および STT モデルを使用した音声エージェントのコード例を確認できます。