---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用レベルへとアップグレードしたものです。Agents SDK はごく少数の基本コンポーネントを提供します。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: あるエージェントが特定のタスクを別のエージェントへ委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を十分に表現でき、急な学習コストなしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグし、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値があるだけの機能は備えるが、学習を素早くするための基本コンポーネントは少数に保つ。
2. そのままでも優れた動作をするが、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能でエージェントのオーケストレーションと連鎖を実現。
- ハンドオフ: 複数のエージェント間の調整と委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断。
- セッション: エージェントの実行をまたぐ会話履歴の自動管理により、手動での状態管理を不要に。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションを提供。
- トレーシング: ワークフローの可視化・デバッグ・モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能な組み込みトレーシング。

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