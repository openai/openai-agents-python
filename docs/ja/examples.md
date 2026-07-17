---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples)の examples セクションでは、SDK のさまざまな実装例を確認できます。コード例は、各種パターンや機能を示す複数のカテゴリーに分類されています。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):** このカテゴリーのコード例では、以下のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   ストリーミングイベントを使用する Agents as tools（`examples/agent_patterns/agents_as_tools_streaming.py`）
    -   構造化入力パラメーターを使用する Agents as tools（`examples/agent_patterns/agents_as_tools_structured.py`）
    -   エージェントの並列実行
    -   条件付きツール使用
    -   異なる動作によるツール使用の強制（`examples/agent_patterns/forcing_tool_use.py`）
    -   入出力ガードレール
    -   判定役としての LLM
    -   ルーティング
    -   ストリーミングガードレール
    -   ツール承認と状態のシリアライズを伴うヒューマンインザループ（`examples/agent_patterns/human_in_the_loop.py`）
    -   ストリーミングを伴うヒューマンインザループ（`examples/agent_patterns/human_in_the_loop_stream.py`）
    -   承認フロー用のカスタム拒否メッセージ（`examples/agent_patterns/human_in_the_loop_custom_rejection.py`）

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):** これらのコード例では、以下のような SDK の基本機能を紹介します。

    -   Hello World のコード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   実行フックとエージェントフックのライフサイクルのコード例（`examples/basic/lifecycle_example.py`）
    -   動的なシステムプロンプト
    -   基本的なツール使用（`examples/basic/tools.py`）
    -   ツールの入出力ガードレール（`examples/basic/tool_guardrails.py`）
    -   画像ツールの出力（`examples/basic/image_tool_output.py`）
    -   出力のストリーミング（テキスト、項目、関数呼び出しの引数）
    -   ターン間で共有セッションヘルパーを使用する Responses WebSocket トランスポート（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用量の追跡
    -   Runner が管理する再試行設定（`examples/basic/retry.py`）
    -   サードパーティ製アダプターを介して Runner が管理する再試行（`examples/basic/retry_litellm.py`）
    -   非厳密な出力型
    -   以前のレスポンス ID の使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):** 航空会社向けカスタマーサービスシステムのコード例です。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):** 財務データ分析用のエージェントとツールを活用した構造化リサーチワークフローを示す、財務リサーチエージェントです。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):** メッセージフィルタリングを伴うエージェントのハンドオフの実践的なコード例です。以下が含まれます:

    -   メッセージフィルターのコード例（`examples/handoffs/message_filter.py`）
    -   ストリーミングを伴うメッセージフィルター（`examples/handoffs/message_filter_streaming.py`）

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):** OpenAI Responses API でホスト型 MCP（Model Context Protocol）を使用する方法を示すコード例です。以下が含まれます:

    -   承認なしのシンプルなホスト型 MCP（`examples/hosted_mcp/simple.py`）
    -   Google Calendar などの MCP コネクター（`examples/hosted_mcp/connectors.py`）
    -   割り込みベースの承認を伴うヒューマンインザループ（`examples/hosted_mcp/human_in_the_loop.py`）
    -   MCP ツール呼び出し用の承認時コールバック（`examples/hosted_mcp/on_approval.py`）

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):** MCP（Model Context Protocol）を使用してエージェントを構築する方法を学べます。以下が含まれます:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE（Server-Sent Events）のコード例
    -   SSE リモートサーバー接続（`examples/mcp/sse_remote_example`）
    -   Streamable HTTP のコード例
    -   Streamable HTTP リモート接続（`examples/mcp/streamable_http_remote_example`）
    -   Streamable HTTP 用のカスタム HTTP クライアントファクトリー（`examples/mcp/streamablehttp_custom_client_example`）
    -   `MCPUtil.get_all_function_tools` を使用したすべての MCP ツールの事前取得（`examples/mcp/get_all_mcp_tools_example`）
    -   FastAPI と MCPServerManager（`examples/mcp/manager_example`）
    -   MCP ツールのフィルタリング（`examples/mcp/tool_filter_example`）

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):** エージェント向けのさまざまなメモリ実装のコード例です。以下が含まれます:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr ステートストアセッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses 圧縮セッションストレージ
    -   `ModelSettings(store=False)` を使用したステートレスな Responses 圧縮（`examples/memory/compaction_session_stateless_example.py`）
    -   ファイルベースのセッションストレージ（`examples/memory/file_session.py`）
    -   ヒューマンインザループを伴うファイルベースのセッション（`examples/memory/file_hitl_example.py`）
    -   ヒューマンインザループを伴う SQLite インメモリセッション（`examples/memory/memory_session_hitl_example.py`）
    -   ヒューマンインザループを伴う OpenAI Conversations セッション（`examples/memory/openai_session_hitl_example.py`）
    -   セッションをまたぐ HITL の承認/拒否シナリオ（`examples/memory/hitl_session_scenario.py`）

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):** カスタムプロバイダーやサードパーティ製アダプターを含め、OpenAI 以外のモデルを SDK で使用する方法を確認できます。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):** SDK を使用してリアルタイム体験を構築する方法を示すコード例です。以下が含まれます:

    -   構造化されたテキストメッセージと画像メッセージを扱う Web アプリケーションパターン
    -   コマンドラインの音声ループと再生処理
    -   WebSocket を介した Twilio Media Streams 連携
    -   Realtime Calls API のアタッチフローを使用する Twilio SIP 連携

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):** 推論コンテンツの扱い方を示すコード例です。以下が含まれます:

    -   Runner API での推論コンテンツ（ストリーミングと非ストリーミング）（`examples/reasoning_content/runner_example.py`）
    -   OpenRouter を介した OSS モデルでの推論コンテンツ（`examples/reasoning_content/gpt_oss_stream.py`）
    -   基本的な推論コンテンツのコード例（`examples/reasoning_content/main.py`）

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):** 複雑なマルチエージェントのリサーチワークフローを示す、シンプルなディープリサーチのクローンです。

- **[sandbox](https://github.com/openai/openai-agents-python/tree/main/examples/sandbox):** 分離されたワークスペースでエージェントを実行するためのコード例です。以下が含まれます:

    -   基本的なサンドボックスエージェントのセットアップ（`examples/sandbox/basic.py`）
    -   Unix ローカルおよび Docker サンドボックスのライフサイクルのコード例
    -   サンドボックスを使用するハンドオフ（`examples/sandbox/handoffs.py`）
    -   サンドボックスのメモリとスナップショットからの再開（`examples/sandbox/memory.py`）
    -   ツールとして公開されるサンドボックスエージェント（`examples/sandbox/sandbox_agents_as_tools.py`）

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):** OpenAI がホストするツールや、以下のような試験的な Codex ツール機能の実装方法を学べます:

    -   Web 検索とフィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   ファイル編集と承認を伴うパッチ適用ツール（`examples/tools/apply_patch.py`）
    -   承認コールバックを伴うシェルツールの実行（`examples/tools/shell.py`）
    -   ヒューマンインザループによる割り込みベースの承認を伴うシェルツール（`examples/tools/shell_human_in_the_loop.py`）
    -   インラインスキルを使用するホスト型コンテナーシェル（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照を使用するホスト型コンテナーシェル（`examples/tools/container_shell_skill_reference.py`）
    -   ローカルスキルを使用するローカルシェル（`examples/tools/local_shell_skill.py`）
    -   名前空間と遅延ツールを使用するツール検索（`examples/tools/tool_search.py`）
    -   構造化ツール呼び出しを並行実行するプログラムによるツール呼び出し（`examples/tools/programmatic_tool_calling.py`）
    -   コンピュータ操作
    -   画像生成
    -   試験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   試験的な Codex の同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):** OpenAI の TTS モデルと STT モデルを使用する音声エージェントのコード例を確認できます。音声ストリーミングのコード例も含まれます。