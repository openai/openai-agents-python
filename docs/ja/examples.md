---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまな実装サンプルをご覧ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、以下のような一般的なエージェント設計パターンを示します

    -   決定論的ワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   入出力のガードレール
    -   LLM を審査員として利用
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例は、SDK の基礎的な機能を示します

    -   Hello world のコード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   動的な system prompt
    -   ストリーミング出力（テキスト、アイテム、function call の引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル / リモート、画像 / PDF）
    -   使用状況の追跡
    -   非厳密な出力型
    -   以前のレスポンス ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のために、エージェントとツールで構造化された調査ワークフローを示す金融リサーチ エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いたエージェントのハンドオフの実用的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) コネクタと承認フローの使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いてエージェントを構築する方法を学べます。以下を含みます:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
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
    カスタムプロバイダーや LiteLLM 統合を含む、非 OpenAI モデルを SDK で使用する方法を学びます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法を示すコード例です。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェイス
    -   Twilio 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学びます:

    -   Web 検索、およびフィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェントのコード例をご覧ください。音声のストリーミングのコード例も含みます。