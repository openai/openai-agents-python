---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番対応版アップグレードです。Agents SDK はごく少数の基本コンポーネントから成ります:

- **エージェント**: instructions とツールを備えた LLM
- **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
- **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持します

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係性を表現でき、急な学習コストなしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK には 2 つの設計原則があります:

1. 使う価値があるだけの機能を備えつつ、学習を容易にするための最小限の基本コンポーネントにとどめること。
2. すぐに使えて高品質に動作しつつ、起きることを正確にカスタマイズできること。

SDK の主な特長は次のとおりです:

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象を学ぶのではなく、言語の組み込み機能を使ってエージェントをオーケストレーションし、連携できます。
- ハンドオフ: 複数のエージェント間で調整・委譲するための強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗した場合は早期に打ち切ります。
- セッション: エージェントの実行をまたいだ会話履歴の自動管理により、手動での状態管理が不要になります。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供します。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にする組み込みのトレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツール群も活用できます。

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