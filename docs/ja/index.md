---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用対応版です。Agents SDK はごく少数の基本コンポーネントで構成されています。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: 複数のエージェント実行にまたがる会話履歴を自動で維持

これらの基本コンポーネントは Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、この SDK には組み込みの ** トレーシング ** が付属し、エージェントのフローを可視化・デバッグし、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能を備えつつ、学習が容易になるよう基本コンポーネントは少数に保つこと。
2. そのままでも高い使い勝手を実現しつつ、挙動を細部までカスタマイズできること。

主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果を LLM に渡す処理、LLM の完了までのループ処理を内蔵。
-   Python ファースト: 新たな抽象化を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションと連結を実現。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力検証やチェックを実行し、失敗時は早期終了。
-   セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、スキーマの自動生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価・ファインチューニング・蒸留ツール群も活用可能。

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

( _これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_ )

```bash
export OPENAI_API_KEY=sk-...
```