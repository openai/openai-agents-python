---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装を確認できます。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを紹介しています。

    -   決定論的ワークフロー
    -   Agents as tools
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   審判としての LLM
    -   ルーティング
    -   ストリーミングガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基本機能を紹介しています。

    -   Hello world のコード例 ( デフォルトモデル、 GPT-5、 open-weight モデル )
    -   エージェントライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力 ( テキスト、項目、関数呼び出し引数 )
    -   ターン間で共有セッションヘルパーを使用する Responses websocket transport (`examples/basic/stream_ws.py`)
    -   プロンプトテンプレート
    -   ファイル処理 ( ローカルおよびリモート、画像および PDF )
    -   使用状況トラッキング
    -   非 strict な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析向けのエージェントとツールを使った構造化リサーチワークフローを示す、金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) コネクターと承認の使用方法を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いたエージェントの構築方法を学べます。内容は次のとおりです。

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP prompt server のコード例
    -   SSE (Server-Sent Events) のコード例
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
    カスタムプロバイダーや LiteLLM 連携を含め、 SDK で OpenAI 以外のモデルを使う方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です。内容は次のとおりです。

    -   構造化テキストおよび画像メッセージを使った Web アプリケーションパターン
    -   コマンドラインの音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 連携
    -   Realtime Calls API attach フローを使用した Twilio SIP 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントリサーチワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OAI hosted tools と、次のような実験的な Codex ツール機能の実装方法を学べます。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   インラインスキル付き hosted container shell (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照付き hosted container shell (`examples/tools/container_shell_skill_reference.py`)
    -   ローカルスキル付き local shell (`examples/tools/local_shell_skill.py`)
    -   namespace と遅延ツールを使う tool search (`examples/tools/tool_search.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex 同一スレッドワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、 TTS と STT モデルを使用した音声エージェントのコード例をご覧ください。