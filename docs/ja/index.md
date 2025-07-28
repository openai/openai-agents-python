---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージにより、エージェント型 AI アプリケーションを構築できるようにします。これは、エージェントに関する以前の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) を本番環境向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**： instructions と tools を備えた LLM です
- **ハンドオフ**：特定のタスクを他のエージェントに委任できます
- **ガードレール**：エージェントへの入力を検証できます
- **セッション**：エージェントの実行間で会話履歴を自動的に維持します

Python と組み合わせることで、これらの基本コンポーネントは tools とエージェント間の複雑な関係を表現でき、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの tracing が付属しており、エージェントのフローを可視化・デバッグできるほか、評価やモデルのファインチューニングにも活用できます。

## Agents SDK を使用する理由

SDK には 2 つの設計原則があります。

1. 使う価値のある十分な機能を備えつつ、学習を早めるために基本コンポーネントは少なくする。  
2. デフォルトで優れた動作を提供しつつ、必要に応じて挙動を細かくカスタマイズできる。

以下は SDK の主な機能です。

- エージェントループ: ツールの呼び出し、結果を LLM に送信、LLM が完了するまでループを実行する処理を内蔵  
- Python ファースト: 新しい抽象を学ぶことなく、言語の標準機能だけでエージェントをオーケストレーションし連携可能  
- ハンドオフ: 複数のエージェント間で調整・委任を行える強力な機能  
- ガードレール: エージェントと並行して入力バリデーションやチェックを実行し、失敗時には早期に処理を終了  
- セッション: エージェント実行間の会話履歴を自動で管理し、手動で状態を保持する手間を排除  
- 関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic ベースのバリデーションを提供  
- トレーシング: ワークフローの可視化・デバッグ・モニタリングを行い、 OpenAI の評価、ファインチューニング、蒸留ツールも活用可能  

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

(_これを実行する場合は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```