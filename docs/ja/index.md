---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を極力減らした軽量で使いやすいパッケージで、エージェント型 AI アプリを構築できるようにします。これはエージェントに関する以前の実験プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版へのアップグレードです。Agents SDK にはごく少数の basic components があります:

-   **エージェント**、instructions と tools を備えた LLM
-   **ハンドオフ**、特定のタスクで他のエージェントに委任できる機能
-   **ガードレール**、エージェントの入力と出力を検証できる機能
-   **セッション**、エージェントの実行間で会話履歴を自動的に維持

これらの basic components は Python と組み合わせることで、ツールとエージェント間の複雑な関係を表現でき、急な学習コストなしに実世界のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が付属し、エージェントのフローを可視化・デバッグできるほか、評価したり、アプリケーション向けにモデルをファインチューニングすることさえ可能です。

## Agents SDK を使う理由

SDK には 2 つの設計原則があります:

1. 使う価値があるだけの十分な機能を提供しつつ、学習を素早くするために basic components は最小限に保つ。
2. すぐに使える状態で優れた体験を提供しつつ、挙動を細部までカスタマイズ可能にする。

SDK の主な機能は次のとおりです:

-   エージェントループ: ツールの呼び出し、実行結果を LLM に送信し、LLM が完了するまでループする処理を内蔵。
-   Python ファースト: 新しい抽象化を学ぶ必要はなく、言語の組み込み機能でエージェントをオーケストレーションし連鎖。
-   ハンドオフ: 複数のエージェント間で調整・委任する強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェント実行間で会話履歴を自動管理し、手動の状態管理を不要に。
-   関数ツール: 任意の Python 関数をツール化し、スキーマ自動生成と Pydantic による検証を提供。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にし、OpenAI の評価、ファインチューニング、蒸留ツール群も利用可能。

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