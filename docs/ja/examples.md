---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例 セクションでは、SDK のさまざまな実装サンプルを確認できます。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、一般的なエージェント の設計パターンを示します。例:

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェント の並列実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   LLM を判定者として使用
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    SDK の基礎的な機能を示すコード例。例:

    -   Hello world コード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェント のライフサイクル管理
    -   動的なシステムプロンプト
    -   ストリーミング 出力（テキスト、アイテム、関数呼び出し引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル・リモート、画像・PDF）
    -   利用状況のトラッキング
    -   非厳密な出力タイプ
    -   前回のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のために、エージェント とツールを用いた構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェント のハンドオフ の実用的な例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型の MCP (Model Context Protocol) コネクタと承認フローの使用例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いたエージェント の構築方法。含まれる内容:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーム可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装のコード例。含まれる内容:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM 連携を含め、非 OpenAI モデルを SDK で使う方法。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法のコード例。含まれる内容:

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示すコード例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント のリサーチ ワークフローを示す、シンプルなディープリサーチ クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツール の実装方法。含まれる内容:

    -   Web 検索 とフィルタ付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェント のコード例（ストリーム配信の音声例を含む）。