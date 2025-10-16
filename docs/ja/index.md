---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型 AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレードです。Agents SDK は、ごく少数の基本コンポーネントを備えています。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他のエージェントに委任できる機能
- **ガードレール**: エージェントの入力と出力の検証を可能にする機能
- **セッション**: エージェントの実行をまたいだ会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントは tools とエージェント間の複雑な関係を表現するのに十分強力であり、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には内蔵の **トレーシング** があり、エージェントのフローを可視化してデバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで実行できます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 つです。

1. 使う価値があるだけの機能を備えつつ、すぐに学べるよう基本コンポーネントは少なくすること。
2. すぐに使える一方で、実際に何が起きるかを細かくカスタマイズできること。

SDK の主な機能は以下のとおりです。

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する内蔵のエージェントループ。
- Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の標準機能でエージェントのオーケストレーションと連携を実現。
- ハンドオフ: 複数のエージェント間の調整と委任を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に停止。
- セッション: エージェントの実行をまたぐ会話履歴の自動管理により、手動の状態管理を不要に。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローを可視化・デバッグ・監視できる内蔵のトレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

## インストール

```bash
pip install openai-agents
```

## Hello World のコード例

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