---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) を使うと、ごく少数の抽象化だけを備えた軽量で使いやすいパッケージで、エージェント型 AI アプリを構築できます。これは、以前のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番対応に進化させたものです。Agents SDK には、ごく少数の基本コンポーネントがあります。

-   **エージェント**。instructions と tools を備えた LLM です
-   **Agents as tools / ハンドオフ**。特定のタスクについて、エージェントがほかのエージェントに委任できるようにします
-   **ガードレール**。エージェントの入力と出力の検証を可能にします

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェントの複雑な関係を表現するのに十分な力を発揮し、学習コストを大きくかけることなく実運用のアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントフローの可視化やデバッグに加えて、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK には、設計上の主要な原則が 2 つあります。

1. 使う価値があるだけの十分な機能を備えつつ、素早く学べるよう基本コンポーネントは少数にとどめること。
2. そのままですぐに使えて、しかも何が起きるかを正確にカスタマイズできること。

以下は、この SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスクが完了するまで継続する組み込みのエージェントループです。
-   **Python ファースト**: 新しい抽象化を学ぶ必要はなく、組み込みの言語機能を使ってエージェントオーケストレーションや連携を行えます。
-   **Agents as tools / ハンドオフ**: 複数のエージェント間で作業を調整および委任するための強力な仕組みです。
-   **Sandbox エージェント**: manifest で定義されたファイル、sandbox client の選択、再開可能な sandbox session を備えた、実際に分離されたワークスペース内で専門エージェントを実行します。
-   **ガードレール**: エージェントの実行と並行して入力検証と安全性チェックを実行し、チェックに通らなかった場合は即座に失敗させます。
-   **関数ツール**: 自動スキーマ生成と Pydantic ベースの検証により、任意の Python 関数をツールに変換します。
-   **MCP サーバーツール呼び出し**: 関数ツールと同じ方法で動作する、組み込みの MCP サーバーツール統合です。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続的なメモリレイヤーです。
-   **Human in the loop**: エージェント実行全体で人間を関与させるための組み込みの仕組みです。
-   **トレーシング**: ワークフローの可視化、デバッグ、監視のための組み込みトレーシングで、OpenAI の評価、ファインチューニング、蒸留ツール群をサポートします。
-   **Realtime Agents**: `gpt-realtime-1.5`、自動割り込み検出、コンテキスト管理、ガードレールなどを使用して、強力な音声エージェントを構築できます。

## Agents SDK と Responses API の比較

この SDK は、OpenAI モデルに対してはデフォルトで Responses API を使用しますが、モデル呼び出しの上により高水準のランタイムを追加します。

次のような場合は、Responses API を直接使用してください。

-   ループ、ツールのディスパッチ、状態管理を自分で扱いたい
-   ワークフローが短命で、主にモデルの応答を返すことが目的である

次のような場合は、Agents SDK を使用してください。

-   ランタイムにターン管理、ツール実行、ガードレール、ハンドオフ、またはセッションを管理させたい
-   エージェントに成果物を生成させたい、または複数の協調したステップにまたがって動作させたい
-   [Sandbox エージェント](sandbox_agents.md) を通じて、実際のワークスペースや再開可能な実行が必要である

どちらか一方を全体で選ぶ必要はありません。多くのアプリケーションでは、管理されたワークフローには SDK を使い、より低水準の経路には Responses API を直接呼び出しています。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください_)

```bash
export OPENAI_API_KEY=sk-...
```

## 開始ポイント

-   [Quickstart](quickstart.md) で最初のテキストベースのエージェントを構築します。
-   次に、[Running agents](running_agents.md#choose-a-memory-strategy) でターン間の状態の持ち方を決めます。
-   タスクが実際のファイル、リポジトリ、またはエージェントごとに分離されたワークスペース状態に依存する場合は、[Sandbox agents quickstart](sandbox_agents.md) を参照してください。
-   ハンドオフと manager 型のオーケストレーションのどちらにするかを決める場合は、[Agent orchestration](multi_agent.md) を参照してください。

## パスの選択

やりたいことは分かっているが、それを説明しているページが分からない場合は、この表を使ってください。

| 目標 | 開始ポイント |
| --- | --- |
| 最初のテキストエージェントを構築し、完全な 1 回の実行を見る | [Quickstart](quickstart.md) |
| 関数ツール、ホストされたツール、または Agents as tools を追加する | [Tools](tools.md) |
| 実際に分離されたワークスペース内で、コーディング、レビュー、またはドキュメント用エージェントを実行する | [Sandbox agents quickstart](sandbox_agents.md) と [Sandbox clients](sandbox/clients.md) |
| ハンドオフと manager 型のエージェントオーケストレーションのどちらにするかを決める | [Agent orchestration](multi_agent.md) |
| ターンをまたいでメモリを維持する | [Running agents](running_agents.md#choose-a-memory-strategy) と [Sessions](sessions/index.md) |
| OpenAI モデル、websocket トランスポート、または OpenAI 以外のプロバイダーを使う | [Models](models/index.md) |
| 出力、実行項目、割り込み、再開状態を確認する | [Results](results.md) |
| `gpt-realtime-1.5` を使った低レイテンシの音声エージェントを構築する | [Realtime agents quickstart](realtime/quickstart.md) と [Realtime transport](realtime/transport.md) |
| speech-to-text / agent / text-to-speech パイプラインを構築する | [Voice pipeline quickstart](voice/quickstart.md) |