---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションでは、SDK のさまざまなサンプル実装を確認できます。これらのコード例は、異なるパターンと機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、次のような一般的なエージェントの設計パターンを示します

    -   決定論的ワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力のガードレール
    -   LLM による評価
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーのコード例は、SDK の基礎的な機能を示します

    -   Hello World のコード例（デフォルトモデル、GPT-5、open-weight model）
    -   エージェントのライフサイクル管理
    -   動的なシステムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況の追跡
    -   厳密でない出力型
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    財務データ分析のために、エージェント と ツール を用いた構造化されたリサーチワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いたエージェントのハンドオフの実用的なコード例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) コネクタと承認フローの使い方を示すコード例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を使ってエージェントを構築する方法を学べます。内容:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーム可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例。内容:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法を紹介。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムの体験を構築するコード例。内容:

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示すコード例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチワークフローを示す、シンプルな ディープリサーチ のクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます:

    -   Web 検索 と フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使った音声エージェントのコード例（音声のストリーミングのコード例を含む）。