---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) の本番運用可能なアップグレード版です。Agents SDK にはごく少数の基本コンポーネントがあります:

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他のエージェントに委譲できる仕組み
- **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
- **セッション**: エージェントの実行をまたいで会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実アプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 つです。

1. 使う価値があるだけの機能を備えつつ、学習が早く済むよう基本コンポーネントは少なくする。
2. そのままでもよく動作し、必要に応じて挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです。

- エージェントループ: ツールの呼び出し、結果の LLM への送信、LLM の完了までのループ処理を内蔵。
- Python ファースト: 新しい抽象を学ぶ必要はなく、言語の標準機能でエージェントのオーケストレーションや連携が可能。
- ハンドオフ: 複数のエージェント間での調整と委譲を実現する強力な機能。
- ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
- セッション: エージェントの実行をまたいだ会話履歴の自動管理により、状態管理の手作業が不要。
- 関数ツール: 任意の Python 関数をツール化し、スキーマの自動生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツール群を利用可能。

## インストール

```bash
pip install openai-agents
```

## Hello World のコード例

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