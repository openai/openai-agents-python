---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、最小限の抽象化で軽量かつ使いやすいパッケージにより、エージェント型 AI アプリを構築できるようにします。これは、以前の エージェント 向け実験である [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けに強化した後継です。Agents SDK はごく少数の基本コンポーネントで構成されています:

-   **Agents**、instructions と tools を備えた LLM
-   **Handoffs**、特定のタスクを他の エージェント に委譲できる仕組み
-   **Guardrails**、エージェントの入力と出力の検証を可能にする仕組み
-   **Sessions**、エージェントの実行間で会話履歴を自動的に維持する仕組み

Python と組み合わせることで、これらの基本コンポーネントはツールと エージェント 間の複雑な関係を表現でき、急な学習曲線なしに実用的なアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属しており、エージェント フローの可視化とデバッグ、評価や、アプリケーションに合わせたモデルの微調整まで行えます。

## Agents SDK の利点

SDK には次の 2 つの設計原則があります:

1. 使う価値があるだけの機能を備えつつ、学習が早いよう基本コンポーネントは少数に保つ。
2. すぐ使える一方で、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです:

-   エージェント ループ: ツール呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の標準機能で エージェント のオーケストレーションや連携を記述可能。
-   ハンドオフ: 複数の エージェント 間での調整と委譲を可能にする強力な機能。
-   ガードレール: 入力の検証とチェックを エージェント と並行実行し、失敗した場合は早期に中断。
-   セッション: エージェント 実行間の会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、スキーマ自動生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視に加え、OpenAI の評価・ファインチューニング・蒸留ツール群を活用可能にする組み込みのトレーシング。

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