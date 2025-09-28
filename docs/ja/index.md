---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する従来の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の、本番運用に向けたアップグレード版です。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と ツール を備えた LLM
-   **ハンドオフ**: 特定のタスクについて、エージェントが他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストの高くない方法で実運用アプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化・デバッグできるほか、評価し、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK は、次の 2 つの設計原則に基づいています。

1. 使う価値のある十分な機能を持ちながら、学習を素早くするために基本コンポーネントは少数に保つこと。
2. すぐに使えて優れた体験を提供しつつ、起きる処理を正確にカスタマイズできること。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのループ。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語に備わった機能でエージェントをオーケストレーションし連鎖させます。
-   ハンドオフ: 複数のエージェント間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗した場合は早期に中断します。
-   セッション: エージェントの実行間での会話履歴を自動管理し、手動での状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供します。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も活用できます。

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