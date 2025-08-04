---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェントベースの AI アプリを構築できます。これは以前のエージェント実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版です。 Agents SDK には、非常に少数の基本コンポーネントがあります:

-   **エージェント**: instructions とツールを備えた LLM
-   **ハンドオフ**: エージェントが特定のタスクを他のエージェントに委任できる
-   **ガードレール**: エージェントへの入力を検証できる
-   **セッション**: エージェントの実行間で会話履歴を自動的に管理する

これらの基本コンポーネントは、 Python と組み合わせることでツールとエージェント間の複雑な関係を表現でき、急な学習コストなしで実用的なアプリケーションを構築できます。さらに、 SDK には組み込みの **tracing** があり、エージェントフローを可視化・デバッグできるほか、評価やファインチューニングにも活用できます。

## Agents SDK を使用する理由

SDK には 2 つの設計原則があります:

1. 使う価値があるだけの機能を備えつつ、プリミティブを絞ることで習得を迅速にすること  
2. すぐに高いパフォーマンスを発揮し、必要に応じて挙動を細かくカスタマイズできること  

主な機能は次のとおりです:

-   Agent loop: ツールの呼び出し、実行結果を LLM に送信し、 LLM が完了するまでループ処理を行う組み込みのエージェントループ
-   Python ファースト: 新しい抽象を学ぶことなく、組み込みの言語機能でエージェントのオーケストレーションとチェーンを実現
-   Handoffs: 複数のエージェント間で協調・委任を行う強力な機能
-   Guardrails: エージェントと並列で入力検証を実行し、失敗した場合は早期に処理を中断
-   Sessions: エージェント実行間の会話履歴を自動管理し、手動での状態管理を排除
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供
-   Tracing: ワークフローを可視化・デバッグ・モニタリングできる組み込みトレーシング機能に加え、 OpenAI の評価・ファインチューニング・ディスティレーションツール群を利用可能

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

(_実行する場合は、 `OPENAI_API_KEY` 環境変数を設定してください_)

```bash
export OPENAI_API_KEY=sk-...
```