---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧いただけます。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを紹介しています。

    -   決定論的ワークフロー
    -   Agents as tools
    -   並列エージェント実行
    -   条件付きツール使用
    -   入力 / 出力ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミングガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基本機能を紹介しています。

    -   Hello World のコード例（デフォルトモデル、GPT-5、open-weight model）
    -   エージェントライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力（text、items、function call args）
    -   ターン間で共有セッションヘルパーを使う Responses websocket transport（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル / リモート、画像 / PDF）
    -   使用状況追跡
    -   Runner 管理の retry 設定（`examples/basic/retry.py`）
    -   LiteLLM を使った Runner 管理の retry（`examples/basic/retry_litellm.py`）
    -   非 strict な出力型
    -   前回 response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを使った構造化された調査ワークフローを示す、financial research agent です。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いたエージェントのハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP（Model context protocol）コネクタと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP（Model context protocol）を使ってエージェントを構築する方法を学べます。内容は次のとおりです。

    -   Filesystem のコード例
    -   Git のコード例
    -   MCP prompt server のコード例
    -   SSE（Server-Sent Events）のコード例
    -   Streamable HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例です。内容は次のとおりです。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr state store セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 統合を含め、SDK で非 OpenAI モデルを使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です。内容は次のとおりです。

    -   構造化 text / image メッセージを使った Web アプリケーションパターン
    -   コマンドライン音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API の attach フローを使った Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチ clone です。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールと実験的な Codex tooling の実装方法を学べます。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付き hosted container shell（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照付き hosted container shell（`examples/tools/container_shell_skill_reference.py`）
    -   ローカルスキル付き local shell（`examples/tools/local_shell_skill.py`）
    -   namespace と deferred tools を使った tool search（`examples/tools/tool_search.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex same-thread ワークフロー（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、TTS / STT モデルを使った音声エージェントのコード例をご覧ください。