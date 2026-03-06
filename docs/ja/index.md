---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で軽量かつ使いやすいパッケージとして、エージェント型 AI アプリを構築できるようにします。これは、エージェント向けの以前の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番対応にアップグレードしたものです。Agents SDK には、ごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **Agents as tools / ハンドオフ**: エージェントが特定のタスクをほかのエージェントに委任できる仕組み
-   **ガードレール**: エージェントの入力と出力を検証できる仕組み

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習コストなしで実運用アプリケーションを構築できます。さらに SDK には組み込みの **トレーシング** があり、エージェントフローの可視化やデバッグ、評価、さらにはアプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には 2 つの主要な設計原則があります。

1. 使う価値があるだけの機能を備えつつ、素早く学べるよう基本コンポーネントは少なく保つこと。
2. そのままですぐに使えて、かつ挙動を細かくカスタマイズできること。

以下が SDK の主な機能です。

-   **エージェントループ**: ツール呼び出しを処理し、結果を LLM に返し、タスク完了まで継続する組み込みループ。
-   **Python ファースト**: 新しい抽象化を学ぶ代わりに、言語組み込み機能でエージェントのオーケストレーションや連携を実現。
-   **Agents as tools / ハンドオフ**: 複数のエージェント間で作業を調整・委任するための強力な仕組み。
-   **ガードレール**: 入力検証と安全性チェックをエージェント実行と並列で実行し、チェックに失敗した場合は早期に停止。
-   **関数ツール**: 任意の Python 関数を、スキーマ自動生成と Pydantic ベースの検証付きツールに変換。
-   **MCP サーバーツール呼び出し**: 関数ツールと同様に動作する、組み込みの MCP サーバーツール連携。
-   **セッション**: エージェントループ内で作業コンテキストを維持するための永続メモリレイヤー。
-   **Human in the loop**: エージェント実行全体に人間を関与させるための組み込みメカニズム。
-   **トレーシング**: ワークフローの可視化・デバッグ・監視のための組み込みトレーシング。OpenAI の評価・ファインチューニング・蒸留ツール群をサポート。
-   **Realtime Agents**: 自動割り込み検知、コンテキスト管理、ガードレールなどの機能を備えた強力な音声エージェントを構築。

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

（これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください）

```bash
export OPENAI_API_KEY=sk-...
```

## 開始地点

-   [Quickstart](quickstart.md) で最初のテキストベースのエージェントを構築します。
-   次に、[Running agents](running_agents.md#choose-a-memory-strategy) でターン間の状態保持方法を決めます。
-   handoffs とマネージャー型オーケストレーションのどちらにするか検討している場合は、[Agent orchestration](multi_agent.md) を参照してください。

## パスの選択

やりたいことは分かっているが、どのページに説明があるか分からない場合はこの表を使ってください。

| 目標 | 開始地点 |
| --- | --- |
| 最初のテキストエージェントを作成し、1 回の完全な実行を確認する | [Quickstart](quickstart.md) |
| 関数ツール、ホストツール、または agents as tools を追加する | [Tools](tools.md) |
| handoffs とマネージャー型オーケストレーションのどちらにするか決める | [Agent orchestration](multi_agent.md) |
| ターン間でメモリを保持する | [Running agents](running_agents.md#choose-a-memory-strategy) と [Sessions](sessions/index.md) |
| OpenAI モデル、websocket トランスポート、または非 OpenAI プロバイダーを使用する | [Models](models/index.md) |
| 出力、実行項目、割り込み、再開状態を確認する | [Results](results.md) |
| 低遅延の音声エージェントを構築する | [Realtime agents quickstart](realtime/quickstart.md) と [Realtime transport](realtime/transport.md) |
| speech-to-text / agent / text-to-speech パイプラインを構築する | [Voice pipeline quickstart](voice/quickstart.md) |