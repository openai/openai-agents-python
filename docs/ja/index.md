---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できます。これは、エージェント向けの以前の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の本番対応アップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクについて、エージェントが他のエージェントへ委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: エージェントの実行間で会話履歴を自動的に維持

これらの基本コンポーネントは **Python** と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェントのフローを可視化してデバッグできるほか、評価や、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

SDK は次の 2 つの設計原則に基づいています。

1. 使う価値がある十分な機能を持ちつつ、学習を素早くするための少数の基本コンポーネントに留めること。
2. そのままでも優れた動作をしつつ、何が起こるかを正確にカスタマイズできること。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象を学ぶ必要なく、言語の組み込み機能でエージェントのオーケストレーションと連結を実現。
- ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、チェックに失敗した場合は早期に中断。
- セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を実施。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能な組み込みトレーシング。

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