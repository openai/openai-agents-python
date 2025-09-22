---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用に適した形にアップグレードしたものです。Agents SDK には、非常に小さな基本コンポーネントのセットがあります。

-   **エージェント**: instructions と ツール を備えた LLM
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力を検証できる仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実用的なアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントフローを可視化・デバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK には、2 つの設計原則があります。

1. 使う価値があるだけの十分な機能を備えつつ、学習を迅速にするために基本コンポーネントは少数に抑える。
2. そのままでも十分に動作し、必要に応じて挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのループ。
-   Python ファースト: 新しい抽象を学ぶ必要なく、言語の組み込み機能でエージェントのオーケストレーションや連携を記述。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価・ファインチューニング・蒸留ツール群も活用できる組み込みのトレーシング。

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

(_これを実行する場合、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```