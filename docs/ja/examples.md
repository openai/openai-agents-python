---
search:
  exclude: true
---
# コード例

[リポジトリ](https://github.com/openai/openai-agents-python/tree/main/examples) の examples セクションで、SDK のさまざまなサンプル実装をご確認ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
  このカテゴリーのコード例は、次のような一般的なエージェント設計パターンを示します。

  - 決定的ワークフロー
  - ツールとしてのエージェント
  - エージェントの並列実行
  - 条件付きのツール使用
  - 入出力ガードレール
  - LLM を審判として
  - ルーティング
  - ストリーミング ガードレール

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
  これらのコード例は、次のような SDK の基礎機能を紹介します。

  - Hello world コード例 (Default model、GPT-5、open-weight model)
  - エージェントのライフサイクル管理
  - 動的なシステムプロンプト
  - ストリーミング出力 (テキスト、アイテム、function call args)
  - プロンプトテンプレート
  - ファイル処理 (ローカルとリモート、画像と PDF)
  - 利用状況のトラッキング
  - 非厳密な出力型
  - 直前のレスポンス ID の利用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
  航空会社向けのカスタマーサービス システムのコード例です。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
  金融データ分析のためのエージェントとツールで、構造化されたリサーチ ワークフローを示す金融リサーチ エージェントです。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
  メッセージフィルタリングを伴うエージェントのハンドオフの実用的なコード例です。

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
  hosted MCP (Model context protocol) コネクタと承認フローの使い方を示すコード例です。

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
  MCP (Model context protocol) を使ってエージェントを構築する方法を学べます。次を含みます:

  - ファイルシステムのコード例
  - Git のコード例
  - MCP プロンプト サーバーのコード例
  - SSE (Server-Sent Events) のコード例
  - ストリーム可能な HTTP のコード例

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
  エージェント向けのさまざまなメモリ実装のコード例です。次を含みます:

  - SQLite セッションストレージ
  - 高度な SQLite セッションストレージ
  - Redis セッションストレージ
  - SQLAlchemy セッションストレージ
  - 暗号化されたセッションストレージ
  - OpenAI セッションストレージ

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
  カスタムプロバイダーや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法を紹介します。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
  SDK を用いてリアルタイム体験を構築するコード例です。次を含みます:

  - Web アプリケーション
  - コマンドライン インターフェイス
  - Twilio 連携

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
  reasoning content と structured outputs の扱い方を示すコード例です。

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
  複雑なマルチエージェントのリサーチ ワークフローを示す、シンプルなディープリサーチ クローンです。

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
  次のような OpenAI がホストするツールの実装方法を学べます。

  - Web 検索、およびフィルター付き Web 検索
  - ファイル検索
  - Code Interpreter
  - コンピュータ操作
  - 画像生成

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
  TTS と STT モデルを用いた音声エージェントのコード例をご覧ください。音声のストリーミング コード例も含みます。