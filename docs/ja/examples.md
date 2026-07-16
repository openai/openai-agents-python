---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例セクションで、SDK のさまざまな実装サンプルをご覧ください。コード例は複数のカテゴリーに整理され、それぞれ異なるパターンと機能を示します。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):** このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   ストリーミングイベントを使用する Agents as tools (`examples/agent_patterns/agents_as_tools_streaming.py`)
    -   構造化入力パラメーターを使用する Agents as tools (`examples/agent_patterns/agents_as_tools_structured.py`)
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   異なる動作でのツール使用の強制 (`examples/agent_patterns/forcing_tool_use.py`)
    -   入出力ガードレール
    -   判定役としての LLM
    -   ルーティング
    -   ストリーミングガードレール
    -   ツール承認と状態のシリアル化を伴う人間参加型フロー (`examples/agent_patterns/human_in_the_loop.py`)
    -   ストリーミングを伴う人間参加型フロー (`examples/agent_patterns/human_in_the_loop_stream.py`)
    -   承認フロー向けのカスタム拒否メッセージ (`examples/agent_patterns/human_in_the_loop_custom_rejection.py`)

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):** これらのコード例では、次のような SDK の基本機能を紹介します。

    -   Hello world のコード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   実行フックとエージェントフックのライフサイクルのコード例 (`examples/basic/lifecycle_example.py`)
    -   動的システムプロンプト
    -   基本的なツールの使用 (`examples/basic/tools.py`)
    -   ツールの入出力ガードレール (`examples/basic/tool_guardrails.py`)
    -   画像形式のツール出力 (`examples/basic/image_tool_output.py`)
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   ターン間で共有されるセッションヘルパーを使用する Responses WebSocket トランスポート (`examples/basic/stream_ws.py`)
    -   プロンプトテンプレート
    -   ファイル処理（ローカルおよびリモート、画像および PDF）
    -   使用量の追跡
    -   Runner が管理する再試行設定 (`examples/basic/retry.py`)
    -   サードパーティー製アダプターを介して Runner が管理する再試行 (`examples/basic/retry_litellm.py`)
    -   厳密でない出力型
    -   以前のレスポンス ID の使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):** 航空会社向けカスタマーサービスシステムのコード例です。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):** エージェントとツールを使用した、金融データ分析向けの構造化された調査ワークフローを示す金融調査エージェントです。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):** メッセージフィルタリングを伴うエージェントのハンドオフの実践的なコード例です。以下が含まれます。

    -   メッセージフィルターのコード例 (`examples/handoffs/message_filter.py`)
    -   ストリーミングを伴うメッセージフィルター (`examples/handoffs/message_filter_streaming.py`)

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):** OpenAI Responses API でホスト型 MCP (Model Context Protocol) を使用する方法を示すコード例です。以下が含まれます。

    -   承認不要のシンプルなホスト型 MCP (`examples/hosted_mcp/simple.py`)
    -   Google Calendar などの MCP コネクター (`examples/hosted_mcp/connectors.py`)
    -   中断ベースの承認を使用する人間参加型フロー (`examples/hosted_mcp/human_in_the_loop.py`)
    -   MCP ツール呼び出しの承認時コールバック (`examples/hosted_mcp/on_approval.py`)

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):** MCP (Model Context Protocol) を使用してエージェントを構築する方法を学びます。以下が含まれます。

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   SSE リモートサーバー接続 (`examples/mcp/sse_remote_example`)
    -   Streamable HTTP のコード例
    -   Streamable HTTP リモート接続 (`examples/mcp/streamable_http_remote_example`)
    -   Streamable HTTP 向けのカスタム HTTP クライアントファクトリー (`examples/mcp/streamablehttp_custom_client_example`)
    -   `MCPUtil.get_all_function_tools` を使用したすべての MCP ツールの事前取得 (`examples/mcp/get_all_mcp_tools_example`)
    -   FastAPI と組み合わせた MCPServerManager (`examples/mcp/manager_example`)
    -   MCP ツールのフィルタリング (`examples/mcp/tool_filter_example`)

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):** エージェント向けのさまざまなメモリ実装のコード例です。以下が含まれます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr ステートストアのセッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses 圧縮セッションストレージ
    -   `ModelSettings(store=False)` を使用したステートレスな Responses 圧縮 (`examples/memory/compaction_session_stateless_example.py`)
    -   ファイルベースのセッションストレージ (`examples/memory/file_session.py`)
    -   人間参加型フローを伴うファイルベースのセッション (`examples/memory/file_hitl_example.py`)
    -   人間参加型フローを伴う SQLite インメモリセッション (`examples/memory/memory_session_hitl_example.py`)
    -   人間参加型フローを伴う OpenAI Conversations セッション (`examples/memory/openai_session_hitl_example.py`)
    -   セッションをまたぐ HITL の承認／拒否シナリオ (`examples/memory/hitl_session_scenario.py`)

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):** カスタムプロバイダーやサードパーティー製アダプターなど、OpenAI 以外のモデルを SDK で使用する方法を紹介します。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):** SDK を使用してリアルタイム体験を構築する方法を示すコード例です。以下が含まれます。

    -   構造化されたテキストメッセージと画像メッセージを使用する Web アプリケーションパターン
    -   コマンドラインでの音声ループと再生処理
    -   WebSocket を介した Twilio Media Streams 統合
    -   Realtime Calls API のアタッチフローを使用した Twilio SIP 統合

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):** 推論コンテンツの扱い方を示すコード例です。以下が含まれます。

    -   Runner API を使用した推論コンテンツ、ストリーミングおよび非ストリーミング (`examples/reasoning_content/runner_example.py`)
    -   OpenRouter 経由の OSS モデルを使用した推論コンテンツ (`examples/reasoning_content/gpt_oss_stream.py`)
    -   基本的な推論コンテンツのコード例 (`examples/reasoning_content/main.py`)

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):** 複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチのクローンです。

- **[sandbox](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox):** 分離されたワークスペースでエージェントを実行するためのコード例です。以下が含まれます。

    -   基本的なサンドボックスエージェントのセットアップ (`examples/sandbox/basic.py`)
    -   Unix ローカルおよび Docker サンドボックスのライフサイクルのコード例
    -   サンドボックスを利用したハンドオフ (`examples/sandbox/handoffs.py`)
    -   サンドボックスのメモリとスナップショットからの再開 (`examples/sandbox/memory.py`)
    -   ツールとして公開されるサンドボックスエージェント (`examples/sandbox/sandbox_agents_as_tools.py`)

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):** OpenAI がホストするツールや実験的な Codex ツール機能の実装方法を学びます。以下が含まれます。

    -   Web 検索、およびフィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   ファイル編集と承認を伴うパッチ適用ツール (`examples/tools/apply_patch.py`)
    -   承認コールバックを伴うシェルツールの実行 (`examples/tools/shell.py`)
    -   中断ベースの人間参加型承認を伴うシェルツール (`examples/tools/shell_human_in_the_loop.py`)
    -   インラインスキルを備えたホスト型コンテナシェル (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照を備えたホスト型コンテナシェル (`examples/tools/container_shell_skill_reference.py`)
    -   ローカルスキルを備えたローカルシェル (`examples/tools/local_shell_skill.py`)
    -   名前空間と遅延ツールを使用したツール検索 (`examples/tools/tool_search.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex の同一スレッドワークフロー (`examples/tools/codex_same_thread.py`)

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):** TTS および STT モデルを使用した音声エージェントのコード例をご覧ください。ストリーミング音声のコード例も含まれます。