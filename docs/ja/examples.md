---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK の多様なサンプル実装をご覧ください。これらのコード例は、さまざまなパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例では、以下のような一般的なエージェントのデザインパターンを示します。

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きツール利用
    -   入出力のガードレール
    -   審判としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例は、以下のような SDK の基礎機能を紹介します。

    -   Hello world examples (Default model, GPT-5, open-weight model)
    -   エージェントのライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   利用状況の追跡
    -   非厳格な出力型
    -   以前のレスポンス ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のために、エージェントとツールで構造化されたリサーチ ワークフローを示す金融リサーチ エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを用いたエージェントのハンドオフの実用例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型 MCP (Model Context Protocol) のコネクタと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いたエージェントの作り方を、以下を含めて学べます。

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーミング可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例です（以下を含む）。

    -   SQLite セッションストレージ
    -   上級 SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタム プロバイダーや LiteLLM 連携を含め、OpenAI 以外のモデルを SDK で使う方法を探ります。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です（以下を含む）。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示す例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複数 エージェント の複雑なリサーチ ワークフローを示す、簡易な ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールの実装方法を学べます。

    -   Web 検索 と フィルター付き Web 検索
    -   ファイル検索
    -   Code interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    音声 エージェント の例をご覧ください。TTS と STT モデルを使用し、ストリーミング音声の例を含みます。