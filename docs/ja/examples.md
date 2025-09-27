---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK の多様なサンプル実装をご覧ください。code examples は、さまざまなパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの code examples は、次のような一般的な エージェント の設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きのツール使用
    -   入出力の ガードレール
    -   LLM を判定者として用いる
    -   ルーティング
    -   ストリーミングの ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの code examples は、SDK の基礎的な機能を示します。

    -   Hello World の code examples（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出し引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況の追跡
    -   厳格でない出力型
    -   以前の response ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    エージェント とツールを用いた金融データ分析のための、構造化されたリサーチワークフローを示す金融リサーチ エージェント です。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴う エージェント の ハンドオフ の実用例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) のコネクタと承認の使い方を示す code examples です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いた エージェント の作り方を学べます。内容:

    -   ファイルシステムの code examples
    -   Git の code examples
    -   MCP プロンプト サーバーの code examples
    -   SSE（Server-Sent Events）の code examples
    -   ストリーム可能な HTTP の code examples

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の code examples。内容:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM との統合を含め、OpenAI 以外のモデルを SDK で使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法を示す code examples。内容:

    -   Web アプリケーション
    -   コマンドラインインターフェイス
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す code examples です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント のリサーチワークフローを実演する、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます。

    -   Web 検索 と フィルタ付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使用した音声 エージェント の code examples。ストリーミング音声の例も含みます。