---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージにより、エージェント的な AI アプリを構築できるようにします。これは、これまでのエージェントに関する実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK はごく少数の基本コンポーネントで構成されています。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**: エージェントの入力と出力を検証できる機能
-   **セッション**: エージェントの実行間で会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用レベルのアプリケーションを構築できます。さらに、この SDK には **トレーシング** が組み込まれており、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値のある十分な機能を備えつつ、学習を素早くできるよう基本コンポーネントは少数に保つこと。
2. すぐに使えて高品質に動作しつつ、挙動を細部までカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのループ。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の標準機能でエージェントのオーケストレーションや連携を記述。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動での状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供。
-   トレーシング: ワークフローの可視化・デバッグ・モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能な組み込みトレーシング。

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