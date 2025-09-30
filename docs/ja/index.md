---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型 AI アプリを構築できます。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用に適したアップグレード版です。Agents SDK にはごく少数の基本コンポーネントがあります:

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクのためにエージェントが他のエージェントへ委譲できる機能
- **ガードレール**: エージェントの入力と出力の検証を可能にする機能
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグ・評価したり、アプリ向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK の設計原則は 2 つあります:

1. 使う価値があるだけの機能は備えつつ、学習を素早くするために基本コンポーネントは少数に保つ。
2. そのままでも十分に使える一方で、挙動を細部までカスタマイズ可能。

主な機能:

- エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループを処理する組み込みのループ。
- Python ファースト: 新しい抽象を学ぶことなく、言語の組み込み機能でエージェントをオーケストレーションし連結できます。
- ハンドオフ: 複数のエージェント間で調整と委譲を行う強力な機能。
- ガードレール: 入力の検証やチェックをエージェントと並行して実行し、失敗時には早期に中断します。
- セッション: エージェントの実行をまたぐ会話履歴を自動管理し、手作業での状態管理を不要にします。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供します。
- トレーシング: ワークフローの可視化・デバッグ・監視に役立つ組み込みのトレーシング。さらに、OpenAI の評価・ファインチューニング・蒸留ツール群を利用できます。

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

(_これを実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```