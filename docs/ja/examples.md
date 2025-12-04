---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、次のような一般的な エージェント の設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェント の並列実行
    -   条件付きのツール使用
    -   入力/出力の ガードレール
    -   審判としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例は、次のような SDK の基本的な機能を紹介します。

    -   Hello World のコード例（デフォルト モデル、GPT-5、オープンウェイト モデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング 出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプト テンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況の追跡
    -   厳密でない出力タイプ
    -   前回のレスポンス ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマー サービス システムのコード例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールで、構造化された調査ワークフローを示す金融調査 エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージ フィルタリングを用いた エージェント のハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホストされた MCP (Model Context Protocol) コネクタと承認の使い方を示すコード例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を用いて エージェント を構築する方法を学べます。内容:

    -   ファイルシステム のコード例
    -   Git のコード例
    -   MCP プロンプト サーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーム可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装のコード例。内容:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタム プロバイダーや LiteLLM との統合を含む、OpenAI 以外のモデルを SDK で使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイムな体験を構築する方法を示すコード例。内容:

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio との統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs を扱う方法を示すコード例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェントのリサーチ ワークフローを示す、シンプルな ディープリサーチ のクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます。

    -   Web 検索 と フィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    TTS と STT モデルを用いた 音声 エージェントのコード例。ストリーミング 音声のコード例も含みます。