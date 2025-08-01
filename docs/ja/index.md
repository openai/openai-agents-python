---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、非常に少ない抽象化で軽量かつ使いやすいパッケージにより、エージェント型 AI アプリを構築できる SDK です。これは、以前のエージェント向け実験プロジェクト [Swarm](https://github.com/openai/swarm/tree/main) を、本番利用向けにアップグレードしたものです。Agents SDK にはごく少数の基本コンポーネントがあります。

- **エージェント**: instructions と tools を備えた LLM  
- **ハンドオフ**: 特定のタスクを他のエージェントへ委任する仕組み  
- **ガードレール**: エージェントへの入力を検証する仕組み  
- **セッション**: エージェント実行間で会話履歴を自動的に保持  

Python と組み合わせることで、これらのコンポーネントは tools と エージェント 間の複雑な関係を表現でき、学習コストを抑えつつ実用的なアプリケーションを構築できます。さらに SDK には、エージェントフローの可視化・デバッグを可能にする **トレーシング** が組み込まれており、評価やファインチューニングまで行えます。

## Agents SDK を使用する理由

SDK の設計原則は次の 2 点です。

1. 利用価値のある十分な機能を持ちつつ、学習が速いようにコンポーネントを最小限に抑える。  
2. デフォルト設定で優れた動作をしつつ、挙動を細部までカスタマイズできる。  

主な機能は次のとおりです。

- エージェントループ: tools の呼び出し、結果を LLM へ送信、LLM が完了するまでのループを自動処理。  
- Python ファースト: 新しい抽象を学ばずに、組み込み言語機能で エージェント をオーケストレーション・連鎖。  
- ハンドオフ: 複数のエージェント間で調整・委任を行う強力な機能。  
- ガードレール: エージェントと並行して入力検証を実行し、失敗時は早期停止。  
- セッション: エージェント実行間の会話履歴を自動管理し、手動での状態管理を不要に。  
- 関数ツール: 任意の Python 関数を tool 化し、スキーマ生成と Pydantic ベースの検証を自動化。  
- トレーシング: フローを可視化・デバッグ・監視でき、OpenAI の評価、ファインチューニング、蒸留ツールも利用可能。  

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

(_実行する際は、環境変数 `OPENAI_API_KEY` を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```