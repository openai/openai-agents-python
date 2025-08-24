---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) のプロダクション対応版アップグレードです。Agents SDK はごく少数の基本コンポーネントで構成されています。

-   **エージェント**、指示とツールを備えた LLM
-   **ハンドオフ**、特定のタスクで他のエージェントに委譲できる機能
-   **ガードレール**、エージェントの入力と出力を検証できる仕組み
-   **セッション**、エージェントの実行間で会話履歴を自動的に維持

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を表現でき、学習コストをかけずに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** が含まれ、エージェントのフローを可視化・デバッグできるほか、評価やアプリケーション向けのモデルのファインチューニングまで行えます。

## Agents SDK を使う理由

この SDK の設計原則は次の 2 点です。

1. 使う価値があるだけの機能を備えつつ、学習が速いよう基本コンポーネントは少数に。
2. そのままでも高品質に動作し、かつ挙動を細部までカスタマイズ可能に。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果を LLM へ送信、LLM が完了するまでのループ処理を内蔵で処理。
-   Python ファースト: 新しい抽象を学ぶのではなく、言語の標準機能を使ってエージェントのオーケストレーションやチェーン化が可能。
-   ハンドオフ: 複数のエージェント間の調整と委譲を実現する強力な機能。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期終了。
-   セッション: エージェントの実行間で会話履歴を自動管理し、手動の状態管理を不要化。
-   関数ツール: 任意の Python 関数をツール化し、自動スキーマ生成と Pydantic によるバリデーションに対応。
-   トレーシング: ワークフローの可視化・デバッグ・監視ができ、加えて OpenAI の評価、ファインチューニング、蒸留ツールのスイートを利用可能。

## インストール

```bash
pip install openai-agents
```

## Hello World サンプル

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