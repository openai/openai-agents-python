---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の code examples セクションで、SDK のさまざまなサンプル実装をご覧ください。code examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの code examples は、次のような一般的な エージェント の設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   並列 エージェント 実行
    -   条件付きツール利用
    -   入出力 ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの code examples は、次のような SDK の基礎機能を紹介します。

    -   Hello World の例 (デフォルトモデル、GPT-5、open-weight モデル)
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力 (テキスト、アイテム、関数呼び出し args)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカル/リモート、画像/PDF)
    -   利用状況トラッキング
    -   非 strict な出力型
    -   以前の response ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステム例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析向けの エージェント とツールを用いた structured なリサーチワークフローを示す、金融リサーチ エージェント です。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いた エージェント の ハンドオフ の実用例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model context protocol) のコネクターと承認の使い方を示す code examples です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model context protocol) を使って エージェント を構築する方法を学びます。内容は次のとおりです。

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプトサーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の code examples です。内容は次のとおりです。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携を含め、SDK で OpenAI 以外のモデルを使う方法を確認します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示す code examples です。内容は次のとおりです。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示す code examples です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント のリサーチワークフローを示す、シンプルな ディープリサーチ のクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツール と、次のような実験的な Codex ツールの実装方法を学びます。

    -   Web 検索 と、フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー (`examples/tools/codex.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声の例を含め、TTS および STT モデルを使用した音声 エージェント の code examples をご覧ください。