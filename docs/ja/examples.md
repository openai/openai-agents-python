---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご覧ください。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの例は、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   並列エージェント実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   審判としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの例は、次のような SDK の基本的な機能を紹介します。

    -   Hello World の例 (デフォルトモデル、GPT-5、open-weight model)
    -   エージェントのライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力 (テキスト、items、function call args)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカルとリモート、画像と PDF)
    -   使用量トラッキング
    -   非厳格な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムの例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた、構造化されたリサーチワークフローを示す金融リサーチ エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを用いた、実用的なエージェントのハンドオフの例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model context protocol) のコネクターと承認の使い方を示す例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model context protocol) を使ってエージェントを構築する方法を学びます。内容は次のとおりです。

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーム可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装の例です。内容は次のとおりです。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 連携を含め、SDK で非 OpenAI モデルを使用する方法を確認します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示す例です。内容は次のとおりです。

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio 連携

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示す例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントによるリサーチワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツール と、次のような実験的な Codex ツール機能の実装方法を学びます。

    -   Web 検索 と、フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキルを備えた Hosted container shell (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照を備えた Hosted container shell (`examples/tools/container_shell_skill_reference.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールのワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex の同一スレッド ワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声の例を含め、TTS および STT モデルを使用する音声エージェントの例をご覧ください。