---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージとして、エージェント型の AI アプリを構築できるようにするものです。これは、以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版アップグレードです。Agents SDK はごく少数の基本コンポーネントで構成されます。

- **エージェント** 、instructions と tools を備えた LLM
- **ハンドオフ** 、特定のタスクを他のエージェントに委任できる機能
- **ガードレール** 、エージェントの入力と出力の検証を可能にする機能
- **セッション** 、エージェントの実行をまたいで会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント の複雑な関係を表現でき、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK には、次の 2 つの設計原則があります。

1. 使う価値があるだけの機能を備えつつ、学習がすばやいように基本コンポーネントは少数に留める。
2. そのままでも優れた体験を提供しつつ、必要に応じて挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を内蔵。
- Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能で エージェント をオーケストレーション・連鎖。
- ハンドオフ: 複数のエージェント間で調整・委任するための強力な機能。
- ガードレール: エージェント と並行して入力のバリデーションやチェックを実行し、失敗時は早期に中断。
- セッション: エージェントの実行をまたいだ会話履歴を自動管理し、手動の状態管理を排除。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供。
- トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能な組み込み機能。

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

(_実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```