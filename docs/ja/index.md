---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化でエージェント型 AI アプリを構築できる軽量で使いやすいパッケージです。これは、以前にエージェント向けに実験していた [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントが含まれています。

-   **エージェント**: instructions と tools を備えた LLM  
-   **ハンドオフ**: 特定のタスクを他のエージェントに委任する仕組み  
-   **ガードレール**: エージェントへの入力を検証する機構  
-   **セッション**: エージェントの実行をまたいで会話履歴を自動管理

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、急な学習コストなく実用的なアプリケーションを構築できます。さらに、SDK には **トレーシング** が組み込まれており、エージェントフローの可視化・デバッグ・評価や、アプリ向けモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には次の 2 つの設計原則があります。

1. 使う価値があるだけの機能を備えつつ、学習しやすいようにコンポーネント数を絞る。  
2. すぐに使い始められる一方で、挙動を細かくカスタマイズできる。

主な機能は以下のとおりです。

-   エージェントループ: ツールの呼び出し、結果を LLM へ送信、LLM が完了するまでのループを自動で処理。  
-   Python ファースト: 新しい抽象を学ばずに Python の言語機能でエージェントを編成・連鎖。  
-   ハンドオフ: 複数エージェント間の協調や委任を実現する強力な機能。  
-   ガードレール: エージェントと並列で入力の検証を実行し、失敗時には早期終了。  
-   セッション: エージェント実行をまたいで会話履歴を自動管理し、状態管理の手間を排除。  
-   関数ツール: 任意の Python 関数を自動スキーマ生成と Pydantic 検証付きのツールに変換。  
-   トレーシング: フローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツールを活用可能。

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

(_これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```