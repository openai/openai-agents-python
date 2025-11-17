---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージにより、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験的プロジェクト [Swarm](https://github.com/openai/swarm/tree/main) を、本番運用に適した形にアップグレードしたものです。Agents SDK には、ごく少数の基本コンポーネントがあります。

-   **エージェント**、LLM に指示とツールを備えたもの
-   **ハンドオフ**、特定のタスクを他のエージェントに委任できる仕組み
-   **ガードレール**、エージェントの入力と出力を検証できる仕組み
-   **セッション**、エージェントの実行をまたいで会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストを抑えつつ実運用のアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価したり、アプリケーション向けにモデルを微調整することもできます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの十分な機能を備えつつ、学習を素早くするために基本コンポーネントは少数に保つ。
2. そのままでも優れた動作をする一方で、必要な挙動を細かくカスタマイズできる。

主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果を LLM へ送信、LLM が完了するまでのループ処理を内蔵。
-   Python ファースト: 新しい抽象を学ぶことなく、言語の標準機能でエージェントのオーケストレーションや連携を実現。
-   ハンドオフ: 複数のエージェント間での調整と委任を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェントの実行をまたいだ会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、スキーマ自動生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みのトレーシング。さらに OpenAI の評価、微調整、蒸留ツール群も活用可能。

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