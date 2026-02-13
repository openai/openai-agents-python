---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧ください。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例は、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   並列エージェント実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   審査員としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基礎的な機能を紹介します。

    -   Hello World コード例 (デフォルトモデル、GPT-5、オープンウェイトモデル)
    -   エージェントのライフサイクル管理
    -   動的なシステムプロンプト
    -   ストリーミング 出力 (テキスト、items、関数呼び出し args)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   使用状況トラッキング
    -   非厳密な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた structured なリサーチワークフローを示す、金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントのハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model context protocol) のコネクターと承認を使用する方法を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    次を含め、MCP (Model context protocol) を用いてエージェントを構築する方法を学びます。

    -   Filesystem コード例
    -   Git コード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) コード例
    -   ストリーミング可能な HTTP コード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    次を含め、エージェント向けのさまざまなメモリ実装のコード例です。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 統合を含め、SDK で OpenAI 以外のモデルを使用する方法を確認します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    次を含め、SDK を使用してリアルタイムの体験を構築する方法を示すコード例です。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs を扱う方法を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのリサーチワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次を含め、OpenAI がホストするツール と、実験的な Codex ツール群を実装する方法を学びます。

    -   Web 検索 と、フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールのワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex 同一スレッドのワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含め、TTS と STT モデルを使用した音声エージェントのコード例をご覧ください。