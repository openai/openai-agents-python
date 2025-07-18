---
search:
  exclude: true
---
# OpenAI Agents SDK

 OpenAI Agents SDK は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント指向の AI アプリを構築できます。これは、エージェントに関する以前の試験的プロジェクトである  Swarm をプロダクションレベルにアップグレードしたものです。 Agents SDK には、ごく少数の基本コンポーネントがあります:

-   **エージェント**:  instructions と tools を備えた  LLM  
-   **ハンドオフ**: 特定のタスクを他のエージェントに委譲できます  
-   **ガードレール**: エージェントへの入力を検証できます  
-   **セッション**: エージェント実行間で会話履歴を自動的に保持します  

 Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストを抑えながら実用的なアプリケーションを構築できます。さらに、SDK には組み込みの  トレーシング があり、エージェントフローの可視化やデバッグ、評価、さらにはアプリ向けモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります。

1. 使う価値があるだけの機能を備えつつ、学習が容易になるよう基本コンポーネントは少なく。  
2. すぐに使い始められる一方で、動作を細部までカスタマイズ可能に。

主な機能は次のとおりです。

-   エージェントループ: ツール呼び出し、結果を  LLM へ送信、 LLM が完了するまでループ処理を自動で行います。  
-   Python ファースト: 新しい抽象概念を学ばずに、言語本来の機能でエージェントをオーケストレーションしチェーン化できます。  
-   ハンドオフ: 複数エージェント間での調整と委譲を実現する強力な機能です。  
-   ガードレール: エージェントと並行して入力バリデーションやチェックを行い、失敗時には早期終了します。  
-   セッション: エージェント実行間の会話履歴を自動で管理し、手動での状態管理を排除します。  
-   関数ツール: 任意の  Python 関数をツール化し、自動スキーマ生成と Pydantic による検証を提供します。  
-   トレーシング: フローの可視化・デバッグ・モニタリングが可能で、 OpenAI の評価、ファインチューニング、蒸留ツールとも連携します。  

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

(_実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```