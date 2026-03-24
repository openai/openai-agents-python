---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で、軽量かつ使いやすいパッケージとしてエージェント型 AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番対応向けにアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **Agents as tools / ハンドオフ**: エージェントが特定タスクのために他のエージェントへ委任できるようにします
-   **ガードレール**: エージェントの入力と出力の検証を可能にします

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現できるほど強力であり、急な学習コストなしに実運用アプリケーションを構築できます。さらに SDK には、エージェント型フローを可視化・デバッグできる組み込みの **トレーシング** があり、評価の実行や、アプリケーション向けモデルのファインチューニングまで可能です。

## Agents SDK を使う理由

SDK には 2 つの中核となる設計原則があります。

1. 使う価値があるだけの十分な機能を持ちつつ、素早く学べるよう基本コンポーネントは少数にすること。
2. そのままですぐに優れた動作をしつつ、何が起きるかを正確にカスタマイズできること。

以下が SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスク完了まで継続する組み込みのエージェントループです。
-   **Python ファースト**: 新しい抽象化を学ぶ必要なく、組み込みの言語機能を使ってエージェントをオーケストレーションし、連結できます。
-   **Agents as tools / ハンドオフ**: 複数エージェント間で作業を調整・委任するための強力な仕組みです。
-   **ガードレール**: エージェント実行と並行して入力検証と安全性チェックを実行し、チェック不合格時には即座に失敗させます。
-   **関数ツール**: 自動スキーマ生成と Pydantic による検証で、任意の Python 関数をツール化できます。
-   **MCP サーバーツール呼び出し**: 関数ツールと同様に動作する、組み込みの MCP サーバーツール統合です。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続メモリレイヤーです。
-   **Human in the loop**: エージェント実行全体で人間を関与させるための組み込みメカニズムです。
-   **トレーシング**: ワークフローの可視化・デバッグ・監視のための組み込みトレーシングで、OpenAI の評価・ファインチューニング・蒸留ツール群をサポートします。
-   **Realtime Agents**: `gpt-realtime-1.5`、自動割り込み検知、コンテキスト管理、ガードレールなどにより、強力な音声エージェントを構築できます。

## インストール

```bash
pip install openai-agents
```

## Hello world 例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

（_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_）

```bash
export OPENAI_API_KEY=sk-...
```

## 開始地点

-   [Quickstart](quickstart.md) で最初のテキストベースエージェントを構築します。
-   次に [Running agents](running_agents.md#choose-a-memory-strategy) で、ターン間で状態をどのように保持するかを決めます。
-   handoffs と manager スタイルのオーケストレーションのどちらにするか検討している場合は、[Agent orchestration](multi_agent.md) を参照してください。

## パスの選択

やりたい作業は分かっているが、どのページに説明があるか分からない場合は、この表を使ってください。

| 目標 | 開始地点 |
| --- | --- |
| 最初のテキストエージェントを構築し、1 回の完全な実行を確認する | [Quickstart](quickstart.md) |
| 関数ツール、ホスト型ツール、または Agents as tools を追加する | [Tools](tools.md) |
| handoffs と manager スタイルのオーケストレーションのどちらにするか決める | [Agent orchestration](multi_agent.md) |
| ターン間でメモリを保持する | [Running agents](running_agents.md#choose-a-memory-strategy) と [Sessions](sessions/index.md) |
| OpenAI モデル、websocket トランスポート、または非 OpenAI プロバイダーを使う | [Models](models/index.md) |
| 出力、実行項目、中断、再開状態を確認する | [Results](results.md) |
| `gpt-realtime-1.5` で低レイテンシの音声エージェントを構築する | [Realtime agents quickstart](realtime/quickstart.md) と [Realtime transport](realtime/transport.md) |
| speech-to-text / agent / text-to-speech パイプラインを構築する | [Voice pipeline quickstart](voice/quickstart.md) |