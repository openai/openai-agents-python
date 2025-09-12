---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の実運用対応版アップグレードです。Agents SDK にはごく少数の基本的なコンポーネントがあります:

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他の エージェント に委譲できる機能
- **ガードレール**: エージェントの入力と出力の検証を可能にします
- **セッション**: エージェントの実行にまたがる会話履歴を自動で保持します

Python と組み合わせることで、これらの基本的なコンポーネントはツールと エージェント の複雑な関係を表現でき、学習コストを抑えつつ実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価やアプリケーション向けのモデルのファインチューニングにも活用できます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 つです:

1. 使う価値があるだけの機能は備えつつ、基本的なコンポーネントは少なく、すぐに学べること。
2. そのままでも十分に使え、必要に応じて挙動をきめ細かくカスタマイズできること。

SDK の主な機能は次のとおりです:

- エージェントループ: ツールの呼び出し、結果を LLM に送信、LLM が完了するまでのループを処理する組み込みのエージェントループ。
- Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能で エージェント をオーケストレーションし連携できます。
- ハンドオフ: 複数の エージェント を協調・委譲させる強力な機能。
- ガードレール: エージェント と並行して入力の検証やチェックを実行し、失敗時は早期に中断します。
- セッション: エージェントの実行にまたがる会話履歴を自動管理し、手動での状態管理を不要にします。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供します。
- トレーシング: ワークフローの可視化、デバッグ、監視を可能にする組み込みのトレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツール群を活用できます。

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