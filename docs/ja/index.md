---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えつつ、軽量で使いやすいパッケージとしてエージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用レベルのアップグレード版です。Agents SDK はごく少数の基本コンポーネントで構成されています。

- **エージェント**: インストラクションとツールを備えた LLM
- **ハンドオフ**: 特定のタスクについて、エージェントが他のエージェントに委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実運用のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェント フローを可視化してデバッグできるほか、評価を行い、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

SDK の設計原則は 2 つあります。

1. 使う価値のある十分な機能を備えつつ、学習がすばやく済むよう基本コンポーネントを少数に保つこと。
2. すぐに使えて優れた動作をしつつ、何が起こるかを正確にカスタマイズできること。

SDK の主な機能は次のとおりです。

- エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM が終了するまでのループ処理を行う組み込みのエージェント ループ。
- Python ファースト: 新しい抽象を学ぶことなく、言語の標準機能でエージェントのオーケストレーションやチェーン化が可能。
- ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期終了。
- セッション: エージェントの実行間で会話履歴を自動管理し、手動での状態管理を不要に。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能。

## インストール

```bash
pip install openai-agents
```

## Hello world のコード例

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