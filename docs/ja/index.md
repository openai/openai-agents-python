---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、少ない抽象化で軽量かつ使いやすいパッケージを通じて、エージェント指向の AI アプリを構築できるようにします。これは、以前のエージェント向け実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番利用向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントが用意されています。

- **エージェント** — instructions と tools を備えた LLM  
- **ハンドオフ** — エージェントが特定タスクを他のエージェントへ委任できます  
- **ガードレール** — エージェントへの入力を検証できます  
- **セッション** — エージェント実行間で会話履歴を自動的に保持します  

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係性を表現でき、学習コストを抑えながら実運用レベルのアプリケーションを構築できます。さらに、SDK にはワークフローを可視化・デバッグできる **トレーシング** が組み込まれており、評価やファインチューニング、蒸留など OpenAI の各種ツールも利用可能です。

## Agents SDK の利点

本 SDK には、次の 2 つの設計原則があります。

1. 使う価値がある十分な機能を備えつつ、学習が速いように基本コンポーネントを最小限にする。  
2. すぐに使える状態で動作する一方、挙動を細かくカスタマイズできる。

主な機能は以下のとおりです。

- **Agent loop**: ツール呼び出し、結果を LLM へ送信、LLM が完了するまでループする処理を内蔵  
- **Python ファースト**: 新しい抽象を覚えることなく、Python の言語機能でエージェントをオーケストレーション・連鎖可能  
- **ハンドオフ**: 複数エージェント間の協調や委任を行える強力な機能  
- **ガードレール**: エージェントと並行して入力検証を実行し、チェック失敗時には早期終了  
- **セッション**: エージェント実行間の会話履歴を自動管理し、手動での状態管理を不要化  
- **Function tools**: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic でのバリデーションを提供  
- **トレーシング**: ワークフローの可視化・デバッグ・モニタリングに加え、OpenAI の評価、ファインチューニング、蒸留ツールを利用可能  

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

(_実行する場合は `OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```