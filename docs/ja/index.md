---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験である [Swarm](https://github.com/openai/swarm/tree/main) の実運用向けアップグレード版です。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions とツールを備えた LLM
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**: エージェントの入力および出力の検証を可能にする機能
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付き、エージェントのフローを可視化・デバッグできるだけでなく、それらを評価し、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK には 2 つの設計原則があります。

1. 使用する価値がある十分な機能を備えつつ、学習が速いよう基本コンポーネントは少数に保つ。
2. すぐに使えて優れた体験を提供しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、組み込みの言語機能でエージェントをオーケストレートし連携できます。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: チェックが失敗した場合は早期に中断しつつ、エージェントと並行して入力の検証やチェックを実行。
-   セッション: エージェントの実行間での会話履歴を自動的に管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視に加えて、OpenAI の評価・ファインチューニング・蒸留ツール群を活用可能な組み込みのトレーシング。

## インストール

```bash
pip install openai-agents
```

## Hello World サンプル

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_If running this, ensure you set the `OPENAI_API_KEY` environment variable_)

```bash
export OPENAI_API_KEY=sk-...
```