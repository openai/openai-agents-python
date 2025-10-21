---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント型 AI アプリを構築できるようにします。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**、instructions と tools を備えた LLM
-   **ハンドオフ**、特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**、エージェントの入力と出力を検証できる機能
-   **セッション**、エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用のアプリケーションを構築できます。さらに、SDK には内蔵の **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能を備えつつ、学習が速いように基本コンポーネントは少なく。
2. すぐに使えて高性能、かつ挙動を細部までカスタマイズ可能。

主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループ処理を内蔵。
-   Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の標準機能でエージェントのオーケストレーションや連鎖が可能。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

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