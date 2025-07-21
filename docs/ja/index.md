---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で軽量かつ使いやすいパッケージとして、エージェント指向の AI アプリを構築できるツールです。これは、以前のエージェント向け実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を、本番環境向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM  
- **ハンドオフ**: 特定タスクを他のエージェントへ委任する仕組み  
- **ガードレール**: エージェントへの入力を検証する仕組み  
- **セッション**: エージェント実行間で会話履歴を自動的に保持  

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には **トレーシング** が組み込まれており、エージェントフローの可視化やデバッグ、評価、さらにはアプリ固有のモデルのファインチューニングも行えます。

## Agents SDK を使用する理由

SDK の設計には 2 つの原則があります。

1. 学ぶ価値のある十分な機能を備えつつ、覚えるべきコンポーネント数は最小限に抑える。  
2. そのままでも優れた体験を提供しつつ、処理内容を細かくカスタマイズできる。

主な特徴は次のとおりです。

- エージェントループ: tools の呼び出し、結果を LLM へ送信、LLM が完了するまでループ処理を自動で実行。  
- Python ファースト: 新しい抽象を学習することなく、Python の言語機能でエージェントをオーケストレーションおよび連鎖。  
- ハンドオフ: 複数エージェント間の調整と委任を可能にする強力な機能。  
- ガードレール: エージェントと並行して入力検証を実行し、チェック失敗時には早期終了。  
- セッション: エージェント実行間の会話履歴を自動管理し、手動での状態管理を排除。  
- 関数ツール: 任意の Python 関数を自動スキーマ生成と Pydantic 検証付きの tool へ変換。  
- トレーシング: ワークフローの可視化・デバッグ・監視だけでなく、OpenAI の評価、ファインチューニング、蒸留ツールも利用可能。

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

(_実行の際は `OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```