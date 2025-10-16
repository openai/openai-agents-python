---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、私たちの以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番運用に対応したアップグレード版です。Agents SDK はごく少数の基本コンポーネントで構成されています:

- **エージェント**: instructions と tools を備えた LLM
- **ハンドオフ**: 特定のタスクを他の エージェント に委任できる仕組み
- **ガードレール**: エージェントの入力と出力の検証を可能にする仕組み
- **セッション**: エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント の複雑な関係を表現でき、急な学習コストなしに実運用アプリケーションを構築できます。さらに、この SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグできるほか、評価や、アプリケーション向けのモデルの微調整まで行えます。

## Agents SDK を使う理由

この SDK は次の 2 つの設計原則に基づいています:

1. 使う価値がある十分な機能を備えつつ、学習を素早くするために基本コンポーネントは少数に保つ。
2. すぐに高品質に動作し、必要に応じて挙動を細かくカスタマイズできる。

SDK の主な機能は次のとおりです:

- エージェントループ: ツールの呼び出し、結果を LLM に渡す処理、LLM が完了するまでのループを内蔵。
- Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能で エージェント をオーケストレーション・連鎖。
- ハンドオフ: 複数の エージェント 間での調整と委任を可能にする強力な機能。
- ガードレール: エージェント と並行して入力の検証やチェックを実行し、失敗時は早期に中断。
- セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要に。
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
- トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価・微調整・蒸留ツール群も活用可能。

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