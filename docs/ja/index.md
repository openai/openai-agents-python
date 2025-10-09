---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレード版です。Agents SDK には非常に少数の基本コンポーネントがあります:

- **エージェント**: 指示とツールを備えた LLM
- **ハンドオフ**: 特定のタスクで他のエージェントに委譲できる機能
- **ガードレール**: エージェントの入力と出力を検証する機能
- **セッション**: 複数のエージェント実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化してデバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能を備えつつ、学習が早く済むよう基本コンポーネントは少数に保つ。
2. すぐに使えて動作が良好でありながら、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェント ループ: ツールの呼び出し、結果を LLM に送信、LLM が完了するまでのループ処理を備えたビルトインのエージェント ループ。
- Python ファースト: 新しい抽象を学ぶ必要はなく、言語の標準機能でエージェントのオーケストレーションや連鎖を実現。
- ハンドオフ: 複数のエージェント間での調整や委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に打ち切り。
- セッション: エージェント実行間の会話履歴を自動管理し、手動での状態管理を不要にします。
- 関数ツール: 任意の Python 関数をツール化し、スキーマを自動生成、Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にする組み込みのトレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

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

( _これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_ )

```bash
export OPENAI_API_KEY=sk-...
```