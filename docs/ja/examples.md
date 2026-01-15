---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧いただけます。コード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、一般的な エージェント の設計パターンを示します。例:

    -   決定論的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きのツール使用
    -   入出力 ガードレール
    -   LLM を判定者として用いる
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーでは、SDK の基礎的な機能を示します。例:

    -   Hello World の例（デフォルト モデル、GPT-5、オープンウェイト モデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプト テンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況のトラッキング
    -   厳密でない出力タイプ
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールにより、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを用いた エージェント のハンドオフの実例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) コネクタと承認の使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を使って エージェント を構築する方法。以下を含みます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の例。以下を含みます:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタム プロバイダーや LiteLLM との統合を含め、OpenAI 以外のモデルを SDK で使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法を示す例。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェイス
    -   Twilio との統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示す例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複数 エージェント による複雑なリサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます:

    -   Web 検索 およびフィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    音声 エージェント の例。TTS と STT モデルを使用し、ストリーミング 音声の例を含みます。