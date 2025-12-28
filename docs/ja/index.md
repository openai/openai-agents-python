---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、以前の エージェント 向け実験である Swarm の本番運用可能なアップグレードです。Agents SDK には、ごく少数の基本コンポーネントがあります:

-   **エージェント** : instructions と tools を備えた LLM
-   **ハンドオフ** : 特定のタスクを他の エージェント に委任できる機能
-   **ガードレール** : エージェント の入力と出力を検証する機能
-   **セッション** : エージェント の実行間で会話履歴を自動的に保持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント 間の複雑な関係を表現するのに十分強力であり、急な学習コストなしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの  **トレーシング**  があり、エージェントのフローを可視化・デバッグできるほか、評価を行い、アプリケーション向けにモデルをファインチューニングすることもできます。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 つです:

1. 使う価値のある十分な機能を備えつつ、学習が速いよう基本コンポーネントは少数に保つ。
2. そのままでも十分に動作しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです:

-   エージェント ループ: tools の呼び出し、結果を LLM へ渡す処理、LLM が完了と判断するまでのループを内蔵。
-   Python ファースト: 新しい抽象化を学ぶのではなく、言語の組み込み機能で エージェント をオーケストレーションおよび連携。
-   ハンドオフ: 複数の エージェント 間の調整と委任を可能にする強力な機能。
-   ガードレール: エージェント と並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェント 実行間の会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、スキーマ自動生成と Pydantic による検証を提供。
-   トレーシング: 組み込みの  トレーシング  でワークフローの可視化・デバッグ・監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能。

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

( _これを実行する場合は、`OPENAI_API_KEY` 環境変数を設定してください_ )

```bash
export OPENAI_API_KEY=sk-...
```