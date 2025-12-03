---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションには、SDK の多様なサンプル実装があります。これらのコード例は、さまざまなパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、一般的なエージェント設計パターンを示します。例えば:

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    このカテゴリーの例では、SDK の基礎的な機能を紹介します。例えば:

    -   Hello World のコード例（デフォルト モデル、GPT-5、open-weight モデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、function call args）
    -   プロンプト テンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況の追跡
    -   厳密でない出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    エージェント とツールを用いた財務データ分析のための、構造化された調査ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを伴うエージェントのハンドオフの実用例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) のコネクタと承認の使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いたエージェントの構築方法を学べます。内容:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例。内容:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタム プロバイダーや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム 体験を構築する方法を示す例。内容:

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント リサーチ ワークフローを示す、ディープリサーチの簡易クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます:

    -   Web 検索 と フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを使った音声 エージェントの例。ストリーミング音声のコード例を含みます。