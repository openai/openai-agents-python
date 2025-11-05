---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装をご覧ください。これらの code examples は、異なるパターンと機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns（エージェント パターン）](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの examples は、以下のような一般的なエージェント設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力のガードレール
    -   LLM を判定者として使用
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic（基本）](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの examples は、以下のような SDK の基礎的な機能を紹介します。

    -   Hello World の code examples（デフォルトモデル、 GPT-5、オープンウェイトのモデル）
    -   エージェントのライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、項目、関数呼び出し引数）
    -   プロンプトテンプレート
    -   ファイル取り扱い（ローカルとリモート、画像と PDF）
    -   利用状況の追跡
    -   厳密でない出力型
    -   以前のレスポンス ID の使用

-   **[customer_service（カスタマーサービス）](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例です。

-   **[financial_research_agent（金融リサーチ エージェント）](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    エージェントとツールを用いた、金融データ分析のための構造化されたリサーチ ワークフローを示す金融リサーチ エージェントです。

-   **[handoffs（ハンドオフ）](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントのハンドオフの実用的な code examples をご覧ください。

-   **[hosted_mcp（ホスト型 MCP）](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型の MCP（Model Context Protocol）コネクタと承認の使用方法を示す examples です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP（Model Context Protocol）でエージェントを構築する方法を学べます。以下を含みます。

    -   ファイルシステムの examples
    -   Git の examples
    -   MCP プロンプト サーバーの examples
    -   SSE（Server-Sent Events）の examples
    -   ストリーム可能な HTTP の examples

-   **[memory（メモリー）](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリー実装の examples です。以下を含みます。

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers（モデルプロバイダー）](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM の統合など、非 OpenAI モデルを SDK で使用する方法を学びます。

-   **[realtime（リアルタイム）](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示す examples。以下を含みます。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content（推論コンテンツ）](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す examples です。

-   **[research_bot（リサーチ ボット）](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント リサーチ ワークフローを示す、シンプルな ディープリサーチ のクローンです。

-   **[tools（ツール）](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学びます。

    -   Web 検索 と フィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice（ボイス）](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェントの examples。ストリーミングされた音声の examples を含みます。