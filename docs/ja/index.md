---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) を使用すると、抽象化をほとんど増やさずに軽量で使いやすいパッケージで エージェント 型の AI アプリを構築できます。これは、以前にエージェント向けに実験的に公開していた [Swarm](https://github.com/openai/swarm/tree/main) の本番運用向けアップグレード版です。Agents SDK には、基本コンポーネントがごく少数しかありません:

- **エージェント**: instructions と tools を備えた LLM  
- **ハンドオフ**: 特定のタスクを他のエージェントに委任  
- **ガードレール**: エージェントへの入力を検証  
- **セッション**: エージェントの実行間で会話履歴を自動保持  

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習曲線なく実用的なアプリを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントフローを可視化・デバッグできるほか、評価やモデルのファインチューニングにも活用できます。

## Agents SDK を使う理由

SDK には次の 2 つの設計原則があります。

1. 使う価値があるだけの機能は備えつつ、学習コストを抑えるために基本コンポーネントの数は最小限にする。  
2. すぐに使えるが、挙動を細かくカスタマイズできる。

主な機能は次のとおりです。

- Agent loop: tools の呼び出し、結果を LLM へ渡す処理、LLM が完了するまでのループを自動で実行。  
- Python ファースト: 新しい抽象を覚えることなく、Python の言語機能でエージェントをオーケストレーション・連鎖。  
- ハンドオフ: 複数のエージェント間で調整・委任を行う強力な機能。  
- ガードレール: エージェントと並行して入力の検証・チェックを実行し、失敗時には早期に停止。  
- セッション: エージェントの実行間で会話履歴を自動管理し、手動で状態を扱う必要を排除。  
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースのバリデーションを提供。  
- トレーシング: ワークフローを可視化・デバッグ・監視できる組み込みトレーシング。さらに OpenAI の評価、ファインチューニング、蒸留ツールを利用可能。  

## インストール

```bash
pip install openai-agents
```

## Hello World 例

```python
from agents import Agent, Runner

agent = Agent(name="Assistant", instructions="You are a helpful assistant")

result = Runner.run_sync(agent, "Write a haiku about recursion in programming.")
print(result.final_output)

# Code within the code,
# Functions calling themselves,
# Infinite loop's dance.
```

(_実行する場合は、必ず `OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```