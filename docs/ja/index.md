---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化をほとんど排した軽量で使いやすいパッケージにより、エージェントベースの AI アプリを構築できるようにします。これは、以前のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) をプロダクションレベルへとアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM  
- **ハンドオフ**: エージェントが特定タスクを他のエージェントへ委任するしくみ  
- **ガードレール**: エージェントへの入力を検証する機能  

Python と組み合わせることで、これらのコンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストを抑えつつ実際のアプリケーションを構築できます。さらに SDK には、エージェントフローを可視化・デバッグできる **トレーシング** が標準搭載されており、評価やファインチューニングにも活用可能です。

## Agents SDK を使用する理由

SDK には 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、学習が早いようコンポーネント数を絞る。  
2. すぐに使い始められる初期設定で動作しつつ、挙動を細かくカスタマイズできる。  

主な機能は次のとおりです。

- エージェントループ: ツール呼び出し、結果を LLM に送信、LLM が完了するまでのループを自動で処理。  
- Python ファースト: 新しい抽象化を学ばずに、言語標準機能でエージェントをオーケストレーション。  
- ハンドオフ: 複数エージェント間の協調と委譲を実現する強力な機能。  
- ガードレール: エージェントと並列で入力バリデーションを実行し、失敗時に早期終了。  
- 関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic での検証を提供。  
- トレーシング: フローの可視化・デバッグ・モニタリングに加え、OpenAI の評価・ファインチューニング・蒸留ツールを利用可能。  

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