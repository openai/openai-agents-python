---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装を確認できます。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、一般的なエージェント設計パターンを示しています。例:

    -   決定論的ワークフロー
    -   Agents as tools
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入力/出力ガードレール
    -   審判としての LLM
    -   ルーティング
    -   ストリーミングガードレール
    -   承認フロー向けのカスタム拒否メッセージ (`examples/agent_patterns/human_in_the_loop_custom_rejection.py`)

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、 SDK の基礎的な機能を紹介しています。例:

    -   Hello world のコード例 (デフォルトモデル、 GPT-5 、 open-weight モデル)
    -   エージェントライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力 (テキスト、項目、関数呼び出し引数)
    -   ターン間で共有セッションヘルパーを使う Responses websocket トランスポート (`examples/basic/stream_ws.py`)
    -   プロンプトテンプレート
    -   ファイル処理 (ローカル/リモート、画像/PDF)
    -   使用状況トラッキング
    -   Runner 管理のリトライ設定 (`examples/basic/retry.py`)
    -   サードパーティアダプターを介した Runner 管理のリトライ (`examples/basic/retry_litellm.py`)
    -   非 strict な出力型
    -   前回レスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを使った構造化リサーチワークフローを示す、金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP (Model Context Protocol) コネクターと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP (Model Context Protocol) を使ってエージェントを構築する方法を学べます。内容:

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE (Server-Sent Events) のコード例
    -   ストリーミング可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例です。内容:

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr ステートストアセッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ
    -   `ModelSettings(store=False)` を使ったステートレスな Responses compaction (`examples/memory/compaction_session_stateless_example.py`)

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーやサードパーティアダプターを含め、 SDK で非 OpenAI モデルを使う方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です。内容:

    -   構造化テキストおよび画像メッセージを使った Web アプリケーションパターン
    -   コマンドライン音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API attach フローを使用した Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェントのディープリサーチワークフローを示す、シンプルなディープリサーチクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツールと、次のような実験的な Codex ツール機能の実装方法を学べます。

    -   Web 検索 とフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付き hosted container shell (`examples/tools/container_shell_inline_skill.py`)
    -   スキル参照付き hosted container shell (`examples/tools/container_shell_skill_reference.py`)
    -   ローカルスキル付きローカル shell (`examples/tools/local_shell_skill.py`)
    -   名前空間と遅延ツールを使ったツール検索 (`examples/tools/tool_search.py`)
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー (`examples/tools/codex.py`)
    -   実験的な Codex 同一スレッドワークフロー (`examples/tools/codex_same_thread.py`)

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、 TTS / STT モデルを使用した音声エージェントのコード例をご覧ください。