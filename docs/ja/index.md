---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これはエージェントに関する以前の実験 [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応のアップグレード版です。Agents SDK には、次の小さな基本コンポーネントのセットがあります:

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを別のエージェントに委任できる機能
-   **ガードレール**: エージェントの入力および出力の検証を可能にする機能
-   **セッション**: エージェント実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストを高めることなく実アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントフローの可視化とデバッグに加えて、評価や、アプリケーション向けモデルのファインチューニングも行えます。

## Agents SDK を使う理由

SDK には次の 2 つの設計原則があります:

1. 使う価値があるだけの十分な機能を備えつつ、学習を速くするために基本コンポーネントは少数であること。
2. すぐに使えて優れた体験を提供しつつ、挙動を細部までカスタマイズできること。

SDK の主な機能は次のとおりです:

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能でエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整と委任を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、チェックが失敗した場合は早期に中断。
-   セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションを提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツールも利用可能。

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