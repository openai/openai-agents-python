---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションには、SDK の多様なサンプル実装があります。これらの code examples は、さまざまなパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの code examples は、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力のガードレール
    -   審査員としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーの code examples では、SDK の基礎的な機能を紹介します。

    -   Hello World の code examples (デフォルトモデル、GPT-5、オープンウェイトのモデル)
    -   エージェントのライフサイクル管理
    -   動的な system prompt
    -   ストリーミング出力 (テキスト、アイテム、関数呼び出しの引数)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   利用状況のトラッキング
    -   非厳密な出力型
    -   前回のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた、構造化されたリサーチワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントのハンドオフの実用的な code examples。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) コネクタと承認フローの活用方法を示す code examples。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) でエージェントを構築する方法を学べます。以下を含みます。

    -   ファイルシステムの code examples
    -   Git の code examples
    -   MCP プロンプトサーバーの code examples
    -   SSE (Server-Sent Events) の code examples
    -   ストリーム可能な HTTP の code examples

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の code examples。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM 連携など、OpenAI 以外のモデルを SDK で使う方法を紹介。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使用してリアルタイム体験を構築する方法の code examples。以下を含みます。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法の code examples。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチワークフローを示す、シンプルな ディープリサーチ のクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールの実装方法を学べます。

    -   Web 検索 と フィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使用した音声エージェントの code examples。ストリーミング音声の code examples を含みます。