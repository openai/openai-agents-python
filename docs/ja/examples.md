---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例セクションでは、SDK の多様なサンプル実装を確認できます。これらのコード例は、さまざまなパターンと機能を示す複数のカテゴリーに整理されています。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
  このカテゴリーのコード例は、一般的なエージェントの設計パターンを示します。

  - 決定論的ワークフロー
  - ツールとしてのエージェント
  - エージェントの並列実行
  - 条件付きのツール使用
  - 入出力ガードレール
  - LLM を裁定者として用いる
  - ルーティング
  - ストリーミング ガードレール

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
  SDK の基礎的な機能を紹介します。

  - Hello World のコード例（デフォルトモデル、GPT-5、open-weight モデル）
  - エージェントのライフサイクル管理
  - 動的なシステムプロンプト
  - ストリーミング出力（テキスト、アイテム、関数呼び出しの引数）
  - プロンプトテンプレート
  - ファイル処理（ローカルとリモート、画像と PDF）
  - 利用状況の追跡
  - 非厳密な出力型
  - 過去の response ID の使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
  航空会社向けのカスタマーサービスシステムの例。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
  金融データ分析のためのエージェントとツールで、構造化されたリサーチワークフローを示す金融リサーチ エージェント。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
  メッセージフィルタリングを用いたエージェントのハンドオフの実用例。

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
  hosted MCP (Model Context Protocol) コネクタと承認の使い方を示すコード例。

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
  MCP (Model Context Protocol) でエージェントを構築する方法を学べます。

  - ファイルシステムのコード例
  - Git のコード例
  - MCP プロンプト サーバーのコード例
  - SSE (Server-Sent Events) のコード例
  - ストリーム可能な HTTP のコード例

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
  エージェント向けのさまざまなメモリ実装のコード例。

  - SQLite セッションストレージ
  - 高度な SQLite セッションストレージ
  - Redis セッションストレージ
  - SQLAlchemy セッションストレージ
  - 暗号化セッションストレージ
  - OpenAI セッションストレージ

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
  カスタムプロバイダーや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
  SDK を用いてリアルタイム体験を構築するコード例。

  - Web アプリケーション
  - コマンドラインインターフェイス
  - Twilio 連携

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
  推論コンテンツと structured outputs の扱い方を示すコード例。

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
  複雑なマルチ エージェントのリサーチワークフローを示す、シンプルなディープリサーチ クローン。

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
  次のような OpenAI がホストするツールの実装方法。

  - Web 検索とフィルター付き Web 検索
  - ファイル検索
  - Code Interpreter
  - コンピュータ操作
  - 画像生成

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
  TTS と STT モデルを用いた音声エージェントのコード例（ストリーミング音声のコード例を含む）。