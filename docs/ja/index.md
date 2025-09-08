---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番対応アップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**: instructions と tools を備えた LLM
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
-   **セッション**: エージェント実行をまたいで会話履歴を自動的に維持

Python と組み合わせると、これらの基本コンポーネントだけでツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価やアプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK の設計原則は 2 つです。

1. 使う価値がある十分な機能を提供しつつ、学習が速いよう基本コンポーネントは少数に保つ。
2. そのままでも優れた体験を提供しつつ、動作を細部までカスタマイズ可能にする。

主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループを処理する組み込みのエージェントループ。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の標準機能でエージェントのオーケストレーションや連携を記述。
-   ハンドオフ: 複数のエージェント間の協調・委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力のバリデーションやチェックを実行し、失敗時は早期に中断。
-   セッション: エージェント実行をまたぐ会話履歴の自動管理により、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツールに変換し、スキーマの自動生成と Pydantic によるバリデーションを提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツール群を活用可能。

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