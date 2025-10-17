---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装をご覧ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、次のような一般的なエージェント設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   入力/出力のガードレール
    -   LLM を審査員として用いる
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーのコード例は、次のような SDK の基礎的な機能を紹介します。

    -   Hello World のコード例 ( Default model、 GPT-5、 open-weight モデル )
    -   エージェントのライフサイクル管理
    -   動的なシステムプロンプト
    -   ストリーミング出力 ( text、 items、 関数呼び出しの引数 )
    -   プロンプトテンプレート
    -   ファイルの取り扱い ( ローカル/リモート、 画像や PDF )
    -   利用状況のトラッキング
    -   厳密でない出力型
    -   前回のレスポンス ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のために、エージェントとツールを用いた構造化された調査ワークフローを示す金融リサーチ エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを使ったエージェントのハンドオフの実用的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型 MCP ( Model Context Protocol ) コネクタと承認フローの使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP ( Model Context Protocol ) を使ってエージェントを構築する方法を学べます。以下を含みます:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE ( Server-Sent Events ) のコード例
    -   ストリーム可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例です。以下を含みます:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM との統合を含め、 OpenAI 以外のモデルを SDK と併用する方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェイス
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルな ディープリサーチ のクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールの実装方法を学べます:

    -   Web 検索、およびフィルタ付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使用した音声エージェントのコード例をご覧ください。音声のストリーミングのコード例も含みます。