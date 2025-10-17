---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK の多様なサンプル実装を参照できます。これらの コード例 は、異なるパターンや機能を示す複数の カテゴリー に整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    この カテゴリー の コード例 は、次のような一般的な エージェント の設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしての エージェント
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入力/出力のガードレール
    -   審査員としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの コード例 は、次のような SDK の基礎的な機能を紹介します。

    -   Hello World の コード例（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェントのライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況の追跡
    -   厳密でない出力タイプ
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールを用いた構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントのハンドオフの実用的な コード例。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型 MCP (Model Context Protocol) のコネクタと承認の使い方を示す コード例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) で エージェント を構築する方法を学べます。内容例:

    -   ファイルシステムの コード例
    -   Git の コード例
    -   MCP プロンプト サーバーの コード例
    -   SSE (Server-Sent Events) の コード例
    -   ストリーム可能な HTTP の コード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の コード例。内容:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携を含む、非 OpenAI モデルを SDK で使う方法を紹介。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を用いてリアルタイムの体験を構築する コード例。内容:

    -   Web アプリケーション
    -   コマンドライン インターフェイス
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    推論コンテンツと structured outputs の扱い方を示す コード例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェントのリサーチ ワークフローを示す、シンプルな deep research クローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツール の実装方法を学べます。

    -   Web 検索 と、フィルタ付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    音声 エージェント の コード例。TTS と STT モデルを使用し、ストリーミング音声の コード例 も含みます。