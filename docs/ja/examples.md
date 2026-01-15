---
search:
  exclude: true
---
# 例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の code examples セクションで、SDK のさまざまな実装例をご覧ください。これらの code examples は、異なるパターンや機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、一般的な エージェント の設計パターンを紹介します。例:

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   LLM を審判として用いる
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーでは、SDK の基礎的な機能を紹介します。例:

    -   Hello World の code examples（既定のモデル、GPT-5、open-weight モデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（text, items, function call args）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況の追跡
    -   非厳密な出力タイプ
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールを用いた構造化されたリサーチ ワークフローを示す、金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴う エージェント の ハンドオフ の実用例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) のコネクタと承認の使い方を示す code examples。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) で エージェント を構築する方法。以下を含みます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装例。以下を含みます:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携など、非 OpenAI モデルを SDK で活用する方法。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法の例。以下を含みます:

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示す code examples。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント のリサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法:

    -   Web 検索 と フィルタ付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声 エージェント の例。ストリーミング音声の code examples を含みます。