---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント型の AI アプリを構築できるようにします。これは、エージェントに関する以前の実験的プロジェクトである [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版です。Agents SDK には非常に少数の基本コンポーネントがあります。

-   **エージェント** 、instructions とツールを備えた LLM
-   **ハンドオフ** 、特定のタスクを他のエージェントに委譲できる仕組み
-   **ガードレール** 、エージェントの入力と出力の検証を可能にする仕組み
-   **セッション** 、エージェントの実行間で会話履歴を自動的に維持する仕組み

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を十分に表現でき、急な学習コストなく実世界のアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化してデバッグできるほか、評価や、アプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの十分な機能を備えつつ、学習を迅速にするため基本コンポーネントは少数に保つ。
2. すぐに使える状態で優れた体験を提供しつつ、挙動を細部までカスタマイズできる。

SDK の主な機能は次のとおりです。

-   エージェントループ: ツールの呼び出し、結果を LLM に渡す処理、LLM が完了するまでのループを組み込みで処理。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の組み込み機能を使ってエージェントのオーケストレーションや連鎖を実現。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時は早期に中断。
-   セッション: エージェント実行間の会話履歴を自動管理し、手動の状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、スキーマの自動生成と Pydantic ベースの検証を提供。
-   トレーシング: ワークフローの可視化、デバッグ、監視に加え、OpenAI の評価、ファインチューニング、蒸留ツール群を活用可能な組み込みトレーシング。

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