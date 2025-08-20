---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント志向の AI アプリを構築できます。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

-  **エージェント**: instructions とツールを備えた LLM
-  **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
-  **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-  **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化・デバッグし、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

SDK の設計原則は 2 つあります。

1. 使う価値があるだけの機能を備えつつ、学習を迅速にするために基本コンポーネントは少数に保つこと。
2. すぐに使えて高性能でありながら、実際の挙動を細かくカスタマイズできること。

主な機能は次のとおりです。

-  エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みループ。
-  Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントをオーケストレーションして連携。
-  ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-  ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
-  セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要にします。
-  関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-  トレーシング: ワークフローの可視化、デバッグ、モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能な組み込みトレーシング。

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