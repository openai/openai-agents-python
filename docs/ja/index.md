---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型 AI アプリを構築できるようにします。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用に耐えるアップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります:

-  **エージェント**: instructions と tools を備えた LLM
-  **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる機能
-  **ガードレール**: エージェントの入力と出力の検証を可能にする機能
-  **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持する機能

これらの基本コンポーネントは、 Python と組み合わせることでツールとエージェント間の複雑な関係性を表現でき、急な学習コストなしに実運用レベルのアプリケーションを構築できます。さらに、 SDK には組み込みの **トレーシング** があり、エージェントフローの可視化とデバッグ、評価や、アプリケーション向けのモデルの微調整まで行えます。

## Agents SDK を使う理由

この SDK には 2 つの設計原則があります:

1. 使う価値があるだけの機能は備えつつ、学習を素早くするために基本コンポーネントは最小限にする。
2. すぐに高品質に動作する一方で、挙動を細部までカスタマイズできる。

主な機能は次のとおりです:

-  エージェントループ: ツールの呼び出し、実行結果を LLM に渡す処理、 LLM の完了までのループを扱う組み込みのループ。
-  Python ファースト: 新しい抽象を学ぶ必要はなく、言語の標準機能でエージェントのオーケストレーションや連携が可能。
-  ハンドオフ: 複数のエージェント間で調整と委譲を行う強力な機能。
-  ガードレール: 入力の検証やチェックをエージェントと並行して実行し、失敗した場合は早期に中断。
-  セッション: エージェント実行間で会話履歴を自動管理し、手動での状態管理を不要に。
-  関数ツール: 任意の Python 関数をツール化し、スキーマを自動生成。 Pydantic による検証も備える。
-  トレーシング: ワークフローの可視化・デバッグ・監視ができ、 OpenAI の評価、微調整、蒸留ツールのスイートも活用可能。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```