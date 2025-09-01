---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型 AI アプリを構築できるようにするものです。これは、エージェントに関する従来の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   ** エージェント ** , `instructions` とツールを備えた LLM
-   ** ハンドオフ ** , エージェントが特定のタスクを他のエージェントに委任できる機能
-   ** ガードレール ** , エージェントの入力と出力を検証できる機能
-   ** セッション ** , エージェントの実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストを抑えつつ実運用レベルのアプリケーションを構築できます。さらに、SDK には内蔵の ** トレーシング ** があり、エージェントのフローを可視化してデバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には次の 2 つの設計原則があります。

1. 使う価値があるだけの機能を備えつつ、学習を素早くするために基本コンポーネントは少数に保つこと。
2. そのままでも十分に機能しつつ、動作を細部までカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループ処理を内蔵。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間の調整と委任を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```