---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションでは、SDK のさまざまなサンプル実装を確認できます。これらのコード例は、異なるパターンや機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns（エージェントのパターン）](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例は、次のような一般的なエージェント設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   入出力のガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic（基礎）](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーでは、SDK の基礎的な機能を次のように紹介します。

    -   Hello World の例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   動的な システムプロンプト
    -   出力のストリーミング（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況のトラッキング
    -   厳密でない出力型
    -   以前のレスポンス ID の利用

-   **[customer_service（カスタマーサービス）](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent（金融リサーチ エージェント）](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールで、構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs（ハンドオフ）](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いたエージェントのハンドオフの実践的な例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) のコネクタと承認の使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) でエージェントを構築する方法。以下を含みます。

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory（メモリ）](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers（モデルプロバイダー）](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携など、OpenAI 以外のモデルを SDK で使用する方法を解説。

-   **[realtime（リアルタイム）](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法を示す例。以下を含みます。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content（推論コンテンツ）](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す例。

-   **[research_bot（リサーチ Bot）](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

-   **[tools（ツール）](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます。

    -   Web 検索 と フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice（音声）](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた音声エージェントの例。ストリーミング音声の例も含みます。