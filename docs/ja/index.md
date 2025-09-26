---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、エージェント向けのこれまでの実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK には、ごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持

Python と組み合わせると、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実世界のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は、次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの十分な機能を備えつつ、学習が速いように基本コンポーネントは少数に保つ。
2. そのままでもうまく動作しつつ、挙動を正確にカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果を LLM へ送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の組み込み機能でエージェントのオーケストレーションや連携が可能。
- ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
- セッション: エージェントの実行をまたいだ会話履歴の自動管理により、手動での状態管理が不要。
- 関数ツール: 任意の Python 関数をツール化し、スキーマ自動生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にする組み込みのトレーシングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

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