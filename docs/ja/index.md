---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関するこれまでの実験的取り組みである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレード版です。Agents SDK には非常に少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委任できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストの高くない方法で実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化してデバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値のある十分な機能を備えつつ、学習を素早くするために基本コンポーネントは最小限に。
2. すぐに使える高い完成度を持ちながら、挙動を細部までカスタマイズ可能に。

主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果を LLM に渡す処理、LLM の完了までのループ処理を内蔵。
-   Python ファースト: 新しい抽象を学ばずに、言語の組み込み機能でエージェントのオーケストレーションや連携を実現。
-   ハンドオフ: 複数のエージェント間での調整や委任を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、モニタリングが可能。さらに OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

## インストール

```bash
pip install openai-agents
```

## Hello World コード例

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