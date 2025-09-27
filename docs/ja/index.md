---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の本番利用可能なアップグレードです。Agents SDK はごく少数の基本コンポーネントで構成されています:

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクについて、エージェントが他のエージェントに委譲できる仕組み
- **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なしに実世界のアプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** が付属しており、エージェント的なフローの可視化やデバッグ、評価、さらにはアプリケーション向けにモデルをファインチューニングすることも可能です。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています。

1. 使う価値が出るだけの機能は備えつつ、学習が速く済むよう基本コンポーネントは少なく。
2. デフォルトでよく動作しつつ、挙動を細部までカスタマイズ可能に。

SDK の主な特徴は次のとおりです。

- エージェントループ: ツールの呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを内蔵で処理。
- Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の組み込み機能でエージェントのオーケストレーションや連携が可能。
- ハンドオフ: 複数のエージェント間での調整や委譲を可能にする強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に打ち切り。
- セッション: エージェントの実行をまたいだ会話履歴を自動管理し、手動の状態管理を不要化。
- 関数ツール: 任意の Python 関数をツール化し、スキーマの自動生成と Pydantic によるバリデーションを提供。
- トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も活用可能。

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