---
search:
  exclude: true
---
# 例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧ください。これらの例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例は、一般的なエージェント設計パターンを示します。例えば:

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   入出力のガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの例は SDK の基礎的な機能を紹介します。例えば:

    -   Hello World の例（デフォルトモデル、GPT-5、オープンウエイトモデル）
    -   エージェントのライフサイクル管理
    -   動的な system prompt
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル/リモート、画像/PDF）
    -   使用状況の追跡
    -   非厳密な出力型
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを伴うエージェントのハンドオフの実用例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) コネクタと承認の使用方法を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いてエージェントを構築する方法。以下を含みます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例。以下を含みます:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使用する方法を紹介。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を用いてリアルタイム体験を構築する方法を示す例。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルなディープリサーチのクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツールの実装方法。例えば:

    -   Web 検索およびフィルター付きの Web 検索
    -   ファイル検索
    -   Code interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェントの例。音声のストリーミングの例を含みます。