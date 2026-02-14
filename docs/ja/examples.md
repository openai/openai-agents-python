---
search:
  exclude: true
---
# 例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご確認ください。examples は、異なるパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
    このカテゴリーの examples では、次のような一般的な エージェント 設計パターンを説明しています。

    -   決定論的ワークフロー
    -   Agents as tools
    -   並列 エージェント 実行
    -   条件付きツール使用
    -   入出力 ガードレール
    -   判定者としての LLM
    -   ルーティング
    -   ストリーミング ガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
    これらの examples では、次のような SDK の基礎的な機能を紹介しています。

    -   Hello World examples（デフォルトモデル、GPT-5、オープンウェイトモデル）
    -   エージェント のライフサイクル管理
    -   動的な システムプロンプト
    -   ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
    -   プロンプトテンプレート
    -   ファイル処理（ローカルおよびリモート、画像および PDF）
    -   使用状況トラッキング
    -   非厳密な出力型
    -   以前の response ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
    航空会社向けのカスタマーサービスシステム例です。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
    金融データ分析のための エージェント とツールを用いた、構造化された調査ワークフローを示す金融調査 エージェント です。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
    メッセージフィルタリングを伴う エージェント のハンドオフの実用例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
    hosted MCP（Model context protocol）コネクターと承認の使い方を示す examples です。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
    MCP（Model context protocol）で エージェント を構築する方法を学べます。以下を含みます。

    -   ファイルシステム examples
    -   Git examples
    -   MCP プロンプト サーバー examples
    -   SSE（Server-Sent Events）examples
    -   ストリーム可能な HTTP examples

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
    エージェント 向けのさまざまなメモリ実装の examples です。以下を含みます。

    -   SQLite セッションストレージ
    -   高度な SQLite セッションストレージ
    -   Redis セッションストレージ
    -   SQLAlchemy セッションストレージ
    -   暗号化されたセッションストレージ
    -   OpenAI セッションストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
    カスタムプロバイダーと LiteLLM 統合を含め、SDK で OpenAI 以外のモデルを使用する方法を確認できます。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
    SDK を使用してリアルタイム体験を構築する方法を示す examples です。以下を含みます。

    -   Web アプリケーション
    -   コマンドラインインターフェース
    -   Twilio 統合
    -   Twilio SIP 統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
    reasoning content と structured outputs を扱う方法を示す examples です。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
    複雑なマルチ エージェント の調査ワークフローを示す、シンプルな ディープリサーチ クローンです。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
    OpenAI がホストするツール と、次のような実験的な Codex ツール機能の実装方法を学べます。

    -   Web 検索 と、フィルター付き Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   インラインスキル付きのホスト型コンテナシェル（`examples/tools/container_shell_inline_skill.py`）
    -   スキル参照付きのホスト型コンテナシェル（`examples/tools/container_shell_skill_reference.py`）
    -   コンピュータ操作
    -   画像生成
    -   実験的な Codex ツールワークフロー（`examples/tools/codex.py`）
    -   実験的な Codex 同一スレッドワークフロー（`examples/tools/codex_same_thread.py`）

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
    ストリーミング音声の examples を含め、TTS および STT モデルを使用した音声 エージェント の examples をご覧ください。