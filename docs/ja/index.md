---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で、軽量かつ使いやすいパッケージとして エージェント型 AI アプリを構築できるようにします。これは、以前の エージェント 向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番対応にアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **Agents as tools / ハンドオフ**: 特定のタスクのために エージェント がほかの エージェント に委任できるようにする仕組み
-   **ガードレール**: エージェント の入力と出力の検証を可能にする仕組み

Python と組み合わせることで、これらの基本コンポーネントは、ツールと エージェント 間の複雑な関係を表現するのに十分強力であり、急な学習コストなしで実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントフローを可視化・デバッグできるほか、それらを評価し、さらにはアプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使用する理由

SDK には 2 つの主要な設計原則があります。

1. 使う価値があるだけの機能を備えつつ、素早く学べるよう基本コンポーネントは少数にすること。
2. そのままで優れた動作をしつつ、何が起こるかを正確にカスタマイズできること。

以下は SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスクが完了するまで継続する組み込みの エージェント ループ。
-   **Python ファースト**: 新しい抽象化を学ぶ必要なく、組み込みの言語機能を使って エージェントオーケストレーション と連結を行えます。
-   **Agents as tools / ハンドオフ**: 複数の エージェント 間で作業を調整・委任するための強力な仕組み。
-   **ガードレール**: エージェント 実行と並行して入力検証と安全性チェックを実行し、チェックを通過しない場合は迅速に失敗させます。
-   **関数ツール**: 自動スキーマ生成と Pydantic ベースの検証により、任意の Python 関数をツールに変換します。
-   **MCP サーバーツール呼び出し**: 関数ツールと同じ方法で動作する、組み込みの MCP サーバーツール統合。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続メモリレイヤー。
-   **Human in the loop**: エージェント 実行全体で人間を関与させるための組み込みメカニズム。
-   **トレーシング**: ワークフローの可視化、デバッグ、監視のための組み込みトレーシング。OpenAI の評価、ファインチューニング、蒸留ツール群をサポートします。
-   **Realtime Agents**: 自動割り込み検出、コンテキスト管理、ガードレールなどの機能を備えた強力な音声 エージェント を構築できます。

## インストール

```bash
pip install openai-agents
```

## Hello World の例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください_）

```bash
export OPENAI_API_KEY=sk-...
```

## 開始ポイント

-   [Quickstart](quickstart.md) で最初のテキストベースの エージェント を構築します。
-   [Realtime agents quickstart](realtime/quickstart.md) で低遅延の音声 エージェント を構築します。
-   代わりに speech-to-text / agent / text-to-speech のパイプラインを使いたい場合は、[Voice pipeline quickstart](voice/quickstart.md) を参照してください。