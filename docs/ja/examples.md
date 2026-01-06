---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装を確認できます。code examples は、異なるパターンや機能を示すいくつかの カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    この カテゴリー の code examples では、以下のような一般的な エージェント の設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きのツール使用
    -   入出力の ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミング の ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    ここでは、SDK の基礎的な機能を示します。

    -   Hello World コード例 (デフォルトモデル、GPT-5、open-weight モデル)
    -   エージェント のライフサイクル管理
    -   動的な system prompt
    -   ストリーミング 出力 (テキスト、アイテム、関数呼び出しの引数)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   利用状況の追跡
    -   厳密でない出力タイプ
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールを使った、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを用いた エージェント の ハンドオフ の実用的な例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) コネクタと承認フローの使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) で エージェント を構築する方法。以下を含みます。

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプト サーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーミング可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の例。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法の例。以下を含みます。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法の例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント リサーチ ワークフローを示す、シンプルな ディープリサーチ のクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツール の実装方法。以下を含みます。

    -   Web 検索 と フィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使った音声 エージェント の例。ストリーミング 音声の code examples も含みます。