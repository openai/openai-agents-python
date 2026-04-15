---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で軽量かつ使いやすいパッケージとして、エージェント型 AI アプリを構築できるようにします。これは、これまでのエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番対応にアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **Agents as tools / ハンドオフ**: 特定のタスクのために、エージェントがほかのエージェントへ委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェントの複雑な関係を表現するのに十分な力を持ち、急な学習コストなしに実運用アプリケーションを構築できます。さらに SDK には、エージェントフローを可視化・デバッグし、評価し、さらにはアプリケーション向けにモデルをファインチューニングできる組み込みの **トレーシング** も備わっています。

## Agents SDK の利用理由

SDK には 2 つの主要な設計原則があります。

1. 使う価値がある十分な機能を持ちつつ、素早く学べるよう基本コンポーネントは少数にすること。
2. そのままですぐに優れた動作をしつつ、何が起きるかを正確にカスタマイズできること。

以下は SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスク完了まで継続する組み込みのエージェントループ。
-   **Python ファースト**: 新しい抽象化を学ぶ代わりに、言語の組み込み機能を使ってエージェントをオーケストレーションおよび連結できます。
-   **Agents as tools / ハンドオフ**: 複数エージェント間で作業を調整・委譲するための強力な仕組み。
-   **Sandbox エージェント**: マニフェストで定義されたファイル、sandbox client の選択、再開可能な sandbox セッションを備えた実際の分離ワークスペース内でスペシャリストを実行します。
-   **ガードレール**: エージェント実行と並列で入力検証と安全性チェックを実行し、チェックを通過しない場合は即座に失敗させます。
-   **関数ツール**: 自動スキーマ生成と Pydantic による検証により、任意の Python 関数をツールに変換します。
-   **MCP サーバーツール呼び出し**: 関数ツールと同様に動作する、組み込みの MCP サーバーツール統合。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続メモリ層。
-   **Human in the loop**: エージェント実行全体で人間を関与させるための組み込みメカニズム。
-   **トレーシング**: ワークフローの可視化・デバッグ・監視のための組み込みトレーシング。OpenAI の評価、ファインチューニング、蒸留ツール群をサポートします。
-   **Realtime エージェント**: `gpt-realtime-1.5`、自動割り込み検出、コンテキスト管理、ガードレールなどを使って強力な音声エージェントを構築できます。

## Agents SDK と Responses API の比較

SDK は OpenAI モデルに対してデフォルトで Responses API を使用しますが、モデル呼び出しの周りにより高レベルなランタイムを追加します。

次の場合は Responses API を直接使います。

-   ループ、ツールディスパッチ、状態処理を自分で管理したい
-   ワークフローが短命で、主にモデルのレスポンスを返すことが目的である

次の場合は Agents SDK を使います。

-   ターン管理、ツール実行、ガードレール、ハンドオフ、またはセッションをランタイムに管理させたい
-   エージェントが成果物を生成する、または複数の連携ステップにわたって動作する必要がある
-   [Sandbox エージェント](sandbox_agents.md) を通じて実際のワークスペースや再開可能な実行が必要である

どちらか 1 つを全体で選ぶ必要はありません。多くのアプリケーションでは、管理されたワークフローには SDK を使い、低レベルな経路には Responses API を直接呼び出します。

## インストール

```bash
pip install openai-agents
```

## Hello World 例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください_)

```bash
export OPENAI_API_KEY=sk-...
```

## 開始地点

-   [Quickstart](quickstart.md) で最初のテキストベースエージェントを構築します。
-   次に、[Running agents](running_agents.md#choose-a-memory-strategy) でターン間の状態保持方法を決めます。
-   タスクが実ファイル、リポジトリ、またはエージェントごとに分離されたワークスペース状態に依存する場合は、[Sandbox エージェント quickstart](sandbox_agents.md) を確認します。
-   ハンドオフとマネージャースタイルのオーケストレーションのどちらにするかを決める場合は、[エージェントオーケストレーション](multi_agent.md) を確認します。

## パスの選択

やりたい作業は分かっているが、どのページに説明があるか分からない場合は、この表を使用してください。

| 目標 | 開始地点 |
| --- | --- |
| 最初のテキストエージェントを構築し、1 回の完全な実行を見る | [Quickstart](quickstart.md) |
| 関数ツール、ホストされたツール、または Agents as tools を追加する | [Tools](tools.md) |
| 実際の分離ワークスペース内でコーディング、レビュー、またはドキュメントエージェントを実行する | [Sandbox エージェント quickstart](sandbox_agents.md) と [Sandbox clients](sandbox/clients.md) |
| ハンドオフとマネージャースタイルのオーケストレーションを比較して決定する | [エージェントオーケストレーション](multi_agent.md) |
| ターン間でメモリを保持する | [Running agents](running_agents.md#choose-a-memory-strategy) と [Sessions](sessions/index.md) |
| OpenAI モデル、websocket トランスポート、または非 OpenAI プロバイダーを使う | [Models](models/index.md) |
| 出力、実行項目、割り込み、再開状態を確認する | [Results](results.md) |
| `gpt-realtime-1.5` を使った低遅延の音声エージェントを構築する | [Realtime agents quickstart](realtime/quickstart.md) と [Realtime transport](realtime/transport.md) |
| speech-to-text / agent / text-to-speech パイプラインを構築する | [Voice pipeline quickstart](voice/quickstart.md) |