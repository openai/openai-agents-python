---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装をご覧ください。examples は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを紹介します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入出力ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミングガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基本機能を紹介します。

    -   Hello world のコード例（デフォルトモデル、 GPT-5、 open-weight モデル）
    -   エージェントのライフサイクル管理
    -   動的システムプロンプト
    -   ストリーミング出力（テキスト、項目、関数呼び出し引数）
    -   ターンをまたいで共有セッションヘルパーを使用する Responses websocket transport（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルおよびリモート、画像および PDF）
    -   使用状況のトラッキング
    -   非 strict な出力型
    -   以前の response ID の利用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のためのエージェントとツールを用いた、構造化された調査ワークフローを示す金融調査エージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを使ったエージェントのハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    ホスト型 MCP（Model Context Protocol）コネクタと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP（Model Context Protocol）を使ってエージェントを構築する方法を学べます。内容は以下を含みます。

    -   Filesystem のコード例
    -   Git のコード例
    -   MCP prompt server のコード例
    -   SSE（Server-Sent Events）のコード例
    -   Streamable HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント向けのさまざまなメモリ実装のコード例です。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr state store セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 統合を含め、 SDK で OpenAI 以外のモデルを使う方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使ってリアルタイム体験を構築する方法を示すコード例です。以下を含みます。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 統合
    -   Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs を扱う方法を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツールや、次のような実験的な Codex ツール機能を実装する方法を学べます。

    -   Web 検索 およびフィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付きホスト型コンテナシェル（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照付きホスト型コンテナシェル（`examples/tools/container_shell_skill_reference.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex 同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、 TTS および STT モデルを使った音声エージェントのコード例をご覧ください。