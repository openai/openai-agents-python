---
search:
  exclude: true
---
# コード例

[repo](https://github.com/openai/openai-agents-python/tree/main/examples) のコード例 セクションで、 SDK のさまざまなサンプル実装をご覧ください。これらのコード例は、異なるパターンや機能を示す複数のカテゴリーに整理されています。

## カテゴリー

-   **[agent_patterns](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns):**  
    このカテゴリーの例では、次のような一般的なエージェントの設計パターンを示します。

    -   決定的なワークフロー
    -   ツールとしてのエージェント
    -   エージェントの並列実行
    -   条件付きのツール使用
    -   入出力のガードレール
    -   審査員としての LLM
    -   ルーティング
    -   ストリーミングのガードレール

-   **[basic](https://github.com/openai/openai-agents-python/tree/main/examples/basic):**  
    このカテゴリーでは、次のような SDK の基礎的な機能を紹介します。

    -   Hello World の例（デフォルトモデル、 GPT-5、オープンウェイト モデル）
    -   エージェントのライフサイクル管理
    -   動的な system prompt
    -   ストリーミング出力（テキスト、アイテム、関数呼び出し引数）
    -   プロンプト テンプレート
    -   ファイル処理（ローカルとリモート、画像と PDF）
    -   使用状況の追跡
    -   非厳密な出力型
    -   以前のレスポンス ID の使用

-   **[customer_service](https://github.com/openai/openai-agents-python/tree/main/examples/customer_service):**  
    航空会社向けのカスタマーサービス システムの例。

-   **[financial_research_agent](https://github.com/openai/openai-agents-python/tree/main/examples/financial_research_agent):**  
    金融データ分析のための、エージェントとツールによる構造化されたリサーチ ワークフローを示す金融リサーチ エージェント。

-   **[handoffs](https://github.com/openai/openai-agents-python/tree/main/examples/handoffs):**  
    メッセージ フィルタリングを伴うエージェントのハンドオフの実用的な例をご覧ください。

-   **[hosted_mcp](https://github.com/openai/openai-agents-python/tree/main/examples/hosted_mcp):**  
    ホスト型 MCP (Model Context Protocol) のコネクタと承認の使い方を示す例。

-   **[mcp](https://github.com/openai/openai-agents-python/tree/main/examples/mcp):**  
    MCP (Model Context Protocol) でエージェントを構築する方法を学べます。内容には次が含まれます:

    -   ファイルシステムの例
    -   Git の例
    -   MCP プロンプト サーバーの例
    -   SSE (Server-Sent Events) の例
    -   ストリーミング可能な HTTP の例

-   **[memory](https://github.com/openai/openai-agents-python/tree/main/examples/memory):**  
    エージェント向けのさまざまなメモリ実装の例。次を含みます:

    -   SQLite セッション ストレージ
    -   高度な SQLite セッション ストレージ
    -   Redis セッション ストレージ
    -   SQLAlchemy セッション ストレージ
    -   暗号化されたセッション ストレージ
    -   OpenAI セッション ストレージ

-   **[model_providers](https://github.com/openai/openai-agents-python/tree/main/examples/model_providers):**  
    カスタム プロバイダーや LiteLLM との統合を含む、 OpenAI 以外のモデルを SDK で使う方法を紹介します。

-   **[realtime](https://github.com/openai/openai-agents-python/tree/main/examples/realtime):**  
    SDK を使ってリアルタイムな体験を構築する方法を示す例。次を含みます:

    -   Web アプリケーション
    -   コマンドライン インターフェース
    -   Twilio との統合

-   **[reasoning_content](https://github.com/openai/openai-agents-python/tree/main/examples/reasoning_content):**  
    推論コンテンツと structured outputs を扱う方法を示す例。

-   **[research_bot](https://github.com/openai/openai-agents-python/tree/main/examples/research_bot):**  
    複数エージェントの複雑なリサーチ ワークフローを示す、シンプルな ディープリサーチ のクローン。

-   **[tools](https://github.com/openai/openai-agents-python/tree/main/examples/tools):**  
    次のような OpenAI がホストするツールの実装方法を学びます:

    -   Web 検索 と フィルター付きの Web 検索
    -   ファイル検索
    -   Code Interpreter
    -   コンピュータ操作
    -   画像生成

-   **[voice](https://github.com/openai/openai-agents-python/tree/main/examples/voice):**  
    TTS と STT のモデルを使用した音声エージェントの例。ストリーミング音声の例も含みます。