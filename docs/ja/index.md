---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で、軽量かつ使いやすいパッケージとしてエージェント型 AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番対応にアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**。instructions と tools を備えた LLM です
-   **Agents as tools / ハンドオフ**。特定のタスクのために、エージェントが他のエージェントへ委任できるようにします
-   **ガードレール**。エージェントの入力と出力の検証を可能にします

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現できるほど強力であり、急な学習曲線なしで実運用アプリケーションを構築できます。さらに SDK には組み込みの **トレーシング** があり、エージェント型フローを可視化してデバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には 2 つの主要な設計原則があります。

1. 使う価値があるだけの機能は備えつつ、すばやく学べるよう基本コンポーネントは少なくすること。
2. そのままですぐに優れた動作をしつつ、何が起こるかを正確にカスタマイズできること。

以下は SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスクが完了するまで継続する組み込みのエージェントループです。
-   **Python ファースト**: 新しい抽象化を学ぶ代わりに、組み込みの言語機能を使ってエージェントをエージェントオーケストレーションおよび連結できます。
-   **Agents as tools / ハンドオフ**: 複数のエージェント間で作業を調整・委任するための強力な仕組みです。
-   **ガードレール**: 入力検証と安全性チェックをエージェント実行と並行して実行し、チェックを通過しない場合は即座に失敗させます。
-   **関数ツール**: 自動スキーマ生成と Pydantic による検証により、任意の Python 関数をツール化できます。
-   **MCP サーバーツール呼び出し**: 関数ツールと同じ方法で動作する、組み込みの MCP サーバーツール統合です。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続的メモリ層です。
-   **Human in the loop**: エージェント実行全体で人間を関与させるための組み込みメカニズムです。
-   **トレーシング**: ワークフローの可視化、デバッグ、監視のための組み込みトレーシングであり、OpenAI の評価、ファインチューニング、蒸留ツール群をサポートします。
-   **Realtime エージェント**: 自動割り込み検知、コンテキスト管理、ガードレールなどの機能を備えた強力な音声エージェントを構築できます。

## インストール

```bash
pip install openai-agents
```

## Hello world の例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定していることを確認してください）

```bash
export OPENAI_API_KEY=sk-...
```

## 開始地点

-   [Quickstart](quickstart.md) で最初のテキストベースのエージェントを構築します。
-   次に、[Running agents](running_agents.md#choose-a-memory-strategy) でターン間の状態をどのように保持するかを決めます。
-   ハンドオフとマネージャー型オーケストレーションのどちらにするかを判断する場合は、[Agent orchestration](multi_agent.md) をご覧ください。

## パスの選択

やりたい作業は分かっているが、どのページに説明があるか分からない場合は、この表を使ってください。

| 目標 | 開始地点 |
| --- | --- |
| 最初のテキストエージェントを構築し、1 回の完全な実行を確認する | [Quickstart](quickstart.md) |
| 関数ツール、ホストされたツール、または Agents as tools を追加する | [Tools](tools.md) |
| ハンドオフとマネージャー型オーケストレーションのどちらにするか決める | [Agent orchestration](multi_agent.md) |
| ターン間でメモリを保持する | [Running agents](running_agents.md#choose-a-memory-strategy) と [Sessions](sessions/index.md) |
| OpenAI モデル、websocket トランスポート、または非 OpenAI プロバイダーを使用する | [Models](models/index.md) |
| 出力、実行項目、割り込み、再開状態を確認する | [Results](results.md) |
| 低レイテンシの音声エージェントを構築する | [Realtime agents quickstart](realtime/quickstart.md) |
| speech-to-text / agent / text-to-speech のパイプラインを構築する | [Voice pipeline quickstart](voice/quickstart.md) |