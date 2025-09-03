---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、当社のエージェントに関する過去の実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番対応版アップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他のエージェントへ委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証できる機能
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を十分に表現でき、学習コストをかけずに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントフローの可視化とデバッグ、評価、そしてアプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能を備えつつ、学習が速いよう基本コンポーネントは少数にする。
2. すぐ使えて優れた体験を提供しつつ、挙動を正確にカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを内蔵。
- Python ファースト: 新しい抽象化を学ぶのではなく、言語の機能を使ってエージェントをオーケストレーションし連鎖できます。
- ハンドオフ: 複数のエージェント間で協調と委譲を行う強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、チェックが失敗したら早期に中断。
- セッション: エージェントの実行をまたいだ会話履歴の自動管理により、手動での状態管理が不要。
- 関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化、デバッグ、監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

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