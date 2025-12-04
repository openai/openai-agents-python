---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験的取り組みである [Swarm](https://github.com/openai/swarm/tree/main) を本番運用向けにアップグレードしたものです。Agents SDK はごく少数の基本コンポーネントで構成されています:

-   **エージェント** , `instructions` と `tools` を備えた LLM
-   **ハンドオフ** , エージェントが特定のタスクを他のエージェントに委譲できる機能
-   **ガードレール** , エージェントの入力と出力を検証できる機能
-   **セッション** , エージェントの実行間で会話履歴を自動的に保持

Python と組み合わせることで、これらの基本コンポーネントは ツール と エージェント の複雑な関係を表現するのに十分強力で、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。加えて、SDK には組み込みの **トレーシング** があり、エージェント フローの可視化とデバッグ、評価、さらにはアプリケーション向けのモデルの微調整まで行えます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 点です:

1. 利用する価値があるだけの機能を備えつつ、基本コンポーネントは少なく習得が速いこと。
2. そのままでも優れた動作をしつつ、挙動を細部までカスタマイズできること。

SDK の主な機能は次のとおりです:

-   エージェント ループ: ツールの呼び出し、結果の LLM への送信、LLM が完了するまでのループ処理を行う組み込みのエージェント ループ。
-   Python ファースト: 新しい抽象を学ぶ必要なく、言語の組み込み機能で エージェント をオーケストレーションし連鎖できます。
-   ハンドオフ: 複数の エージェント 間の調整と委譲を可能にする強力な機能。
-   ガードレール: エージェント と並行して入力の検証とチェックを実行し、チェックが失敗した場合は早期に中断。
-   セッション: エージェントの実行間での会話履歴管理を自動化し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視を可能にする組み込みのトレーシングに加え、OpenAI の評価、微調整、蒸留ツール群を利用できます。

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