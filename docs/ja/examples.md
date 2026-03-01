---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装をご覧ください。 examples は、異なるパターンと機能を示すいくつかのカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例は、次のような一般的な エージェント 設計パターンを示します。

    -   決定的なワークフロー
    -   Agents as tools
    -   並列 エージェント 実行
    -   条件付きツール使用
    -   入出力 ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの例は、次のような SDK の基礎的な機能を紹介します。

    -   Hello World の例 (Default model、 GPT-5、 open-weight model)
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング 出力 (text、 items、 function call args)
    -   ターンをまたいで共有セッションヘルパーを使う Responses websocket transport (`examples/basic/stream_ws.py`)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   使用状況トラッキング
    -   非 strict な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステム例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールを用いた、構造化されたリサーチワークフローを示す金融リサーチ エージェント です。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴う エージェント の ハンドオフ の実践例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model context protocol) コネクターと承認の使い方を示す例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    次を含め、 MCP (Model context protocol) を用いた エージェント の構築方法を学びます。

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプトサーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    次を含む、 エージェント 向けのさまざまなメモリ実装の例です。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携を含め、 SDK で非 OpenAI モデルを使用する方法を確認します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    次を含め、 SDK を使ってリアルタイム体験を構築する方法を示す例です。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 連携
    -   Twilio SIP 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示す例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント のリサーチワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次を含む OpenAI がホストするツール と、実験的な Codex ツール機能の実装方法を学びます。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付き hosted container shell (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照付き hosted container shell (`examples/tools/container_shell_skill_reference.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールのワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex の同一スレッドワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声の例を含め、当社の TTS および STT モデルを使用する音声 エージェント の例をご覧ください。