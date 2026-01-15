---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント駆動の AI アプリを構築できるようにするものです。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレード版です。Agents SDK はごく少数の基本コンポーネントから成ります。

-   **エージェント**: instructions と tools を備えた LLMs
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール**: エージェントの入力と出力の検証を可能にする機能
-   **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は 2 つの設計原則に基づいています。

1. 使う価値がある十分な機能を備えつつ、学習を素早くするための最小限の基本コンポーネント。
2. すぐに使える体験を提供しつつ、実際に起こることを細部までカスタマイズ可能。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツール呼び出し、結果を LLM に送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整や委譲を可能にする強力な機能。
-   ガードレール: 入力の検証やチェックをエージェントと並行して実行し、失敗時は早期に中断。
-   セッション: エージェントの実行をまたいだ会話履歴の自動管理により、手動の状態管理が不要。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も活用できる組み込みのトレーシング。

## インストール

```bash
pip install openai-agents
```

## Hello world の例

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