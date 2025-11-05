---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番利用向けアップグレードです。Agents SDK はごく少数の基本コンポーネントで構成されています:

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを別のエージェントに委譲可能にします
-   **ガードレール**: エージェントの入力と出力の検証を可能にします
-   **セッション**: エージェントの実行間で会話履歴を自動的に保持します

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用のアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** が用意されており、エージェントのフローを可視化・デバッグし、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています:

1. 使う価値があるだけの機能を備えつつ、学習を素早くするために基本コンポーネントは少数に保つ。
2. すぐに使えて高品質に動作しつつ、動作内容を正確にカスタマイズできる。

主な機能は次のとおりです:

-   エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのループ。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションや連携を実現。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動での状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価・ファインチューニング・蒸留ツール群も活用可能な組み込み機能。

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