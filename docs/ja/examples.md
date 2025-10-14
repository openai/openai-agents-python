---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例セクションにある SDK のさまざまなサンプル実装をご覧ください。これらのコード例は、異なるパターンや機能を示すいくつかのカテゴリーに整理されています。

## カテゴリー

- **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**
  このカテゴリーのコード例は、次のような一般的なエージェント設計パターンを示します。

  - 決定的なワークフロー
  - ツールとしてのエージェント
  - エージェントの並列実行
  - 条件付きのツール使用
  - 入出力のガードレール
  - 判定者としての LLM
  - ルーティング
  - ストリーミング ガードレール

- **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**
  このカテゴリーのコード例は、次のような SDK の基礎的な機能を紹介します。

  - Hello World のコード例（既定のモデル、GPT-5、オープンウェイトのモデル）
  - エージェントのライフサイクル管理
  - 動的な システムプロンプト
  - ストリーミング 出力（テキスト、アイテム、関数呼び出し引数）
  - プロンプトテンプレート
  - ファイル処理（ローカルとリモート、画像と PDF）
  - 利用状況の追跡
  - 非厳密な出力型
  - 以前のレスポンス ID の使用

- **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**
  航空会社向けのカスタマーサービス システムの例。

- **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**
  エージェント と ツール を用いた金融データ分析のための構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

- **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**
  メッセージフィルタリング付きのエージェントのハンドオフの実例をご覧ください。

- **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**
  ホスト型の MCP (Model Context Protocol) コネクタと承認の使い方を示すコード例。

- **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**
  MCP (Model Context Protocol) でエージェントを構築する方法を学べます。次を含みます:

  - ファイルシステムのコード例
  - Git のコード例
  - MCP プロンプト サーバーのコード例
  - SSE (Server-Sent Events) のコード例
  - ストリーミング可能な HTTP のコード例

- **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**
  エージェント向けのさまざまなメモリ実装のコード例。次を含みます:

  - SQLite セッションストレージ
  - 高度な SQLite セッションストレージ
  - Redis セッションストレージ
  - SQLAlchemy セッションストレージ
  - 暗号化セッションストレージ
  - OpenAI セッションストレージ

- **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**
  カスタムプロバイダーや LiteLLM 連携を含む、OpenAI 以外のモデルを SDK で使う方法を紹介します。

- **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**
  SDK を使ってリアルタイム体験を構築する方法を示すコード例。次を含みます:

  - Web アプリケーション
  - コマンドライン インターフェイス
  - Twilio 連携

- **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**
  推論コンテンツと structured outputs の扱い方を示すコード例。

- **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**
  複雑な複数のエージェント によるリサーチ ワークフローを示す、シンプルな ディープリサーチ クローン。

- **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**
  次のような OpenAI がホストするツール の実装方法を学べます。

  - Web 検索 および フィルター付き Web 検索
  - ファイル検索
  - Code Interpreter
  - コンピュータ操作
  - 画像生成

- **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**
  TTS と STT モデルを用いた音声 エージェントのコード例。音声のストリーミング のコード例も含みます。