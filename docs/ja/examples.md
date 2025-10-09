---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションにある、 SDK の多様なサンプル実装をご確認ください。これらの code examples は、さまざまなパターンや機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、一般的な エージェント の設計パターンを示します。例えば、

    -   決定論的ワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きのツール使用
    -   入力/出力 ガードレール
    -    LLM を審査員として利用
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーの例では、次のような SDK の基礎的な機能を紹介します。

    -   Hello World の code examples（Default model、 GPT-5、 open-weight model）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出し引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカル/リモート、画像/PDF）
    -   利用状況の追跡
    -   非厳格な出力型
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールで、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージのフィルタリングを用いた エージェント の ハンドオフ の実用的な例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型 MCP (Model Context Protocol) コネクタと承認フローの使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いた エージェント の構築方法。以下を含みます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -    SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
     エージェント 向けのさまざまなメモリ実装の例。以下を含みます:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化セッション ストレージ
    -    OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタム プロバイダーや LiteLLM 連携を含む、非 OpenAI モデルを SDK で使う方法。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法の例。以下を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェイス
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント リサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法:

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使用した音声 エージェント の例。ストリーミング音声の例を含みます。