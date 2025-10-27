---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例は、次のような一般的なエージェント設計パターンを示します

    -   決定論的ワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力のガードレール
    -   LLM を判定者として使用
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    SDK の基礎的な機能を示すコード例です

    -   Hello World のコード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出し引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況のトラッキング
    -   厳格でない出力型
    -   前回のレスポンス ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    エージェントとツールを用いた金融データ分析のための、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリング付きのエージェントのハンドオフの実用例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) コネクタと承認フローの使用方法を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を使ってエージェントを構築する方法。以下を含みます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーミング可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例。以下を含みます:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    OpenAI 以外のモデルを SDK で使う方法を紹介。カスタムプロバイダーや LiteLLM 連携を含みます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示す例。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示すコード例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールの実装方法:

    -   Web 検索 と フィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェントの例。ストリーミング音声のコード例を含みます。