---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧いただけます。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、次のような一般的なエージェント設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力のガードレール
    -   LLM による判定
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーのコード例では、SDK の基礎的な機能を紹介します。

    -   Hello World のコード例（デフォルトモデル、GPT-5、open-weight モデル）
    -   エージェントのライフサイクル管理
    -   動的な system prompt
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル/リモート、画像/PDF）
    -   利用状況の追跡
    -   厳密ではない出力型
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けの顧客サービスシステムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のために、エージェントとツールを用いた構造化されたリサーチ ワークフローを示す金融リサーチ エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを伴うエージェントのハンドオフの実用例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) のコネクタと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) でエージェントを構築する方法を学べます。内容:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプト サーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーミング可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けの多様なメモリ実装のコード例です。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM 連携を含め、OpenAI 以外のモデルを SDK で使用する方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムの体験を構築する方法を示すコード例です。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複数エージェントによる高度なリサーチ ワークフローを示す、シンプルなディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールの実装方法を学べます。

    -   Web 検索とフィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    当社の TTS と STT モデルを用いた音声エージェントの例で、音声のストリーミング コード例も含みます。