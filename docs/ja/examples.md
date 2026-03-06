---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装を確認できます。examples は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの examples では、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   エージェントの並列実行
    -   条件付きツール利用
    -   入出力ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミングガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの examples では、次のような SDK の基本機能を紹介します。

    -   Hello World の code examples（デフォルトモデル、 GPT-5、 open-weight モデル）
    -   エージェントのライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力（テキスト、項目、関数呼び出し引数）
    -   ターンをまたいで共有セッションヘルパーを使う Responses websocket transport（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用量トラッキング
    -   非 strict な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを使った構造化された調査ワークフローを示す、金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントハンドオフの実践的な examples を確認できます。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP（Model context protocol）コネクタと承認の使い方を示す examples です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP（Model context protocol）を使ったエージェントの構築方法を学べます。内容は次のとおりです。

    -   ファイルシステムの examples
    -   Git の examples
    -   MCP プロンプトサーバーの examples
    -   SSE（Server-Sent Events）の examples
    -   ストリーミング可能な HTTP の examples

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の examples です。内容は次のとおりです。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr state store セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 統合を含め、 SDK で non-OpenAI モデルを使う方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使用してリアルタイム体験を構築する方法を示す examples です。内容は次のとおりです。

    -   構造化テキストと画像メッセージを使う Web アプリケーションパターン
    -   コマンドラインでの音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API の attach フローを使う Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示す examples です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OAI hosted tools と実験的な Codex ツール機能の実装方法を学べます。内容は次のとおりです。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付き hosted container shell（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照付き hosted container shell（`examples/tools/container_shell_skill_reference.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex 同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声 examples を含め、 TTS と STT モデルを使った音声エージェントの examples を確認できます。