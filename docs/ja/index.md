---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用レベルにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他のエージェントに委譲可能にする機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: エージェント実行間で会話履歴を自動的に維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェント フローの可視化とデバッグ、評価、さらにはアプリケーション向けモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、少数の基本コンポーネントで素早く学べること。
2. すぐに使えて優れた体験を提供しつつ、動作を細部までカスタマイズできること。

SDK の主な機能は次のとおりです。

- エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を内蔵。
- Python ファースト: 新しい抽象概念を学ぶのではなく、言語組み込み機能でエージェントのオーケストレーションと連鎖を実現。
- ハンドオフ: 複数エージェント間の調整と委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力検証やチェックを実行し、失敗時は早期に中断。
- セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を排除。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化、デバッグ、モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

## インストール

```bash
pip install openai-agents
```

## Hello world のサンプル

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