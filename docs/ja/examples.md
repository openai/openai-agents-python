---
search:
  exclude: true
---
# コード例

SDK のさまざまなサンプル実装は、[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例セクションで確認できます。これらのコード例は、さまざまなパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):** このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   ストリーミングイベントを伴う Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   構造化された入力パラメーターを伴う Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   エージェントの並列実行
    -   条件付きツール使用
    -   異なる動作でツール使用を強制（`examples/agent_patterns/forcing_tool_use.py`）
    -   入出力ガードレール
    -   LLM を評価者として使用
    -   ルーティング
    -   ストリーミングガードレール
    -   ツール承認と状態のシリアライズを伴う人間参加型フロー（`examples/agent_patterns/human_in_the_loop.py`）
    -   ストリーミングを伴う人間参加型フロー（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   承認フロー向けのカスタム拒否メッセージ（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):** これらのコード例では、次のような SDK の基礎的な機能を紹介します。

    -   Hello World のコード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   実行フックとエージェントフックのライフサイクルコード例（`examples/basic/lifecycle_example.py`）
    -   動的システムプロンプト
    -   基本的なツール使用（`examples/basic/tools.py`）
    -   ツールの入出力ガードレール（`examples/basic/tool_guardrails.py`）
    -   画像ツール出力（`examples/basic/image_tool_output.py`）
    -   ストリーミング出力（テキスト、項目、関数呼び出し引数）
    -   ターンをまたいだ共有セッションヘルパーを使用する Responses WebSocket トランスポート（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況トラッキング
    -   Runner 管理の再試行設定（`examples/basic/retry.py`）
    -   サードパーティアダプター経由の Runner 管理の再試行（`examples/basic/retry_litellm.py`）
    -   非厳密な出力型
    -   以前のレスポンス ID の使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):** 航空会社向けのカスタマーサービスシステムのコード例です。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):** 金融データ分析向けのエージェントとツールを使った、構造化されたリサーチワークフローを示す金融調査エージェントです。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):** メッセージフィルタリングを含む、エージェントのハンドオフの実践的なコード例です。内容は次のとおりです。

    -   メッセージフィルターのコード例（`examples/handoffs/message_filter.py`）
    -   ストリーミングを伴うメッセージフィルター（`examples/handoffs/message_filter_streaming.py`）

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):** OpenAI Responses API でホスト型 MCP (Model Context Protocol) を使用する方法を示すコード例です。内容は次のとおりです。

    -   承認なしのシンプルなホスト型 MCP（`examples/hosted_mcp/simple.py`）
    -   Google Calendar などの MCP コネクター（`examples/hosted_mcp/connectors.py`）
    -   割り込みベースの承認を伴う人間参加型フロー（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP ツール呼び出しに対する承認時コールバック（`examples/hosted_mcp/on_approval.py`）

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):** MCP (Model Context Protocol) でエージェントを構築する方法を学べます。内容は次のとおりです。

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   SSE リモートサーバー接続（`examples/mcp/sse_remote_example`）
    -   Streamable HTTP のコード例
    -   Streamable HTTP リモート接続（`examples/mcp/streamable_http_remote_example`）
    -   Streamable HTTP 用のカスタム HTTP クライアントファクトリ（`examples/mcp/streamablehttp_custom_client_example`）
    -   `MCPUtil.get_all_function_tools` によるすべての MCP ツールのプリフェッチ（`examples/mcp/get_all_mcp_tools_example`）
    -   FastAPI を使用した MCPServerManager（`examples/mcp/manager_example`）
    -   MCP ツールのフィルタリング（`examples/mcp/tool_filter_example`）

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):** エージェント向けのさまざまなメモリ実装のコード例です。内容は次のとおりです。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr ステートストアセッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses コンパクションセッションストレージ
    -   `ModelSettings(store=False)` を使用したステートレスな Responses コンパクション（`examples/memory/compaction_session_stateless_example.py`）
    -   ファイルバックエンドのセッションストレージ（`examples/memory/file_session.py`）
    -   人間参加型フローを伴うファイルバックエンドのセッション（`examples/memory/file_hitl_example.py`）
    -   人間参加型フローを伴う SQLite インメモリセッション（`examples/memory/memory_session_hitl_example.py`）
    -   人間参加型フローを伴う OpenAI Conversations セッション（`examples/memory/openai_session_hitl_example.py`）
    -   セッションをまたぐ HITL 承認/拒否シナリオ（`examples/memory/hitl_session_scenario.py`）

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):** カスタムプロバイダーやサードパーティアダプターを含め、SDK で OpenAI 以外のモデルを使用する方法を確認できます。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):** SDK を使用してリアルタイム体験を構築する方法を示すコード例です。内容は次のとおりです。

    -   構造化テキストメッセージと画像メッセージを使用する Web アプリケーションパターン
    -   コマンドラインの音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API のアタッチフローを使用した Twilio SIP 統合

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):** 推論内容を扱う方法を示すコード例です。内容は次のとおりです。

    -   Runner API による推論内容、ストリーミングおよび非ストリーミング（`examples/reasoning_content/runner_example.py`）
    -   OpenRouter 経由の OSS モデルによる推論内容（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基本的な推論内容のコード例（`examples/reasoning_content/main.py`）

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):** 複雑なマルチエージェントリサーチワークフローを示す、シンプルなディープリサーチのクローンです。

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):** OpenAI がホストするツールと、次のような実験的な Codex ツール機能の実装方法を学べます。

    -   Web 検索とフィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   ファイル編集と承認を伴う Apply patch ツール（`examples/tools/apply_patch.py`）
    -   承認コールバックを伴うシェルツール実行（`examples/tools/shell.py`）
    -   割り込みベースの承認を伴う人間参加型シェルツール（`examples/tools/shell_human_in_the_loop.py`）
    -   インラインスキルを伴うホスト型コンテナーシェル（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照を伴うホスト型コンテナーシェル（`examples/tools/container_shell_skill_reference.py`）
    -   ローカルスキルを伴うローカルシェル（`examples/tools/local_shell_skill.py`）
    -   名前空間と遅延ツールを伴うツール検索（`examples/tools/tool_search.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex 同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):** ストリーミング音声のコード例を含む、当社の TTS および STT モデルを使用した音声エージェントのコード例を確認できます。