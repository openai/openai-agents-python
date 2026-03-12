---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、 SDK のさまざまなサンプル実装をご確認ください。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーのコード例では、次のような一般的なエージェント設計パターンを示します。

    -   決定論的ワークフロー
    -   Agents as tools
    -   エージェントの並列実行
    -   条件付きツール使用
    -   入力/出力ガードレール
    -   審査者としての LLM
    -   ルーティング
    -   ストリーミングガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらのコード例では、次のような SDK の基本機能を紹介します。

    -   Hello World のコード例（デフォルトモデル、 GPT-5、 open-weight モデル）
    -   エージェントのライフサイクル管理
    -   動的なシステムプロンプト
    -   ストリーミング出力（テキスト、項目、関数呼び出し引数）
    -   ターン間で共有セッションヘルパーを使用する Responses websocket トランスポート（`examples/basic/stream_ws.py`）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルおよびリモート、画像および PDF）
    -   使用状況トラッキング
    -   Runner 管理の再試行設定（`examples/basic/retry.py`）
    -   LiteLLM を使用した Runner 管理の再試行（`examples/basic/retry_litellm.py`）
    -   非 strict 出力型
    -   前回レスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステムのコード例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析向けに、エージェントとツールを使った構造化された調査ワークフローを示す金融リサーチエージェントです。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴うエージェントのハンドオフの実践的なコード例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP（Model context protocol）コネクタと承認の使い方を示すコード例です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    次を含め、 MCP（Model context protocol）でエージェントを構築する方法を学べます。

    -   ファイルシステムのコード例
    -   Git のコード例
    -   MCP プロンプトサーバーのコード例
    -   SSE（Server-Sent Events）のコード例
    -   ストリーム可能な HTTP のコード例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    次を含む、エージェント向けのさまざまなメモリ実装のコード例です。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   Dapr state store セッションストレージ
    -   暗号化セッションストレージ
    -   OpenAI Conversations セッションストレージ
    -   Responses compaction セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーや LiteLLM 統合を含め、 SDK で非 OpenAI モデルを使う方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    次を含め、 SDK を使用してリアルタイム体験を構築する方法を示すコード例です。

    -   構造化テキストおよび画像メッセージを使った Web アプリケーションパターン
    -   コマンドラインの音声ループと再生処理
    -   WebSocket 経由の Twilio Media Streams 統合
    -   Realtime Calls API attach フローを使用した Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs の扱い方を示すコード例です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチエージェント調査ワークフローを示す、シンプルなディープリサーチクローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    次のような OpenAI がホストするツールと実験的な Codex ツール群の実装方法を学べます。

    -   Web 検索とフィルター付き Web 検索
    -   ファイル検索
    -   コードインタープリター
    -   インラインスキルを使用した hosted container shell（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照を使用した hosted container shell（`examples/tools/container_shell_skill_reference.py`）
    -   ローカルスキルを使用した local shell（`examples/tools/local_shell_skill.py`）
    -   名前空間と遅延ツールを使用したツール検索（`examples/tools/tool_search.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex 同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声のコード例を含む、 TTS および STT モデルを使用した音声エージェントのコード例をご覧ください。