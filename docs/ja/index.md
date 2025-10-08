---
search:
  exclude: true
---
# OpenAI Agents SDK

[OpenAI Agents SDK](https://github.com/openai/openai-agents-python) は、抽象化を最小限に抑えた軽量で使いやすいパッケージで、エージェント的な AI アプリを構築できるようにします。これは、以前のエージェント向け実験である [Swarm](https://github.com/openai/swarm/tree/main) の本番対応版アップグレードです。Agents SDK にはごく少数の基本コンポーネントがあります。

-   **エージェント**、instructions と tools を備えた LLM
-   **ハンドオフ**、特定のタスクでエージェントが他のエージェントに委譲できる機能
-   **ガードレール**、エージェントの入力と出力の検証を可能にする機能
-   **セッション**、エージェントの実行をまたいで会話履歴を自動で維持する機能

Python と組み合わせることで、これらの基本コンポーネントはツールとエージェント間の複雑な関係を十分に表現でき、急な学習曲線なしに実運用レベルのアプリケーションを構築できます。さらに、SDK には組み込みの **トレーシング** があり、エージェントのフローを可視化・デバッグでき、評価やアプリケーション向けモデルのファインチューニングにも活用できます。

## Agents SDK を使う理由

この SDK には 2 つの設計原則があります。

1. 使う価値があるだけの機能は備えるが、学習が速いよう基本コンポーネントの数は少なく。
2. そのままでも高い品質で動作し、必要に応じて挙動を細かくカスタマイズ可能。

SDK の主な機能は次のとおりです。

-   エージェント ループ: ツールの呼び出し、結果を LLM に渡す処理、LLM が完了するまでのループを内蔵で処理します。
-   Python ファースト: 新しい抽象を学ぶ必要はなく、言語の組み込み機能でエージェントをオーケストレーションし、連結できます。
-   ハンドオフ: 複数のエージェント間での調整と委譲を可能にする強力な機能です。
-   ガードレール: エージェントと並行して入力の検証やチェックを実行し、失敗時には早期に中断できます。
-   セッション: エージェントの実行をまたいだ会話履歴を自動管理し、手動の状態管理を不要にします。
-   関数ツール: 任意の Python 関数をツールに変換し、自動スキーマ生成と Pydantic ベースの検証を提供します。
-   トレーシング: ワークフローの可視化・デバッグ・監視を可能にする組み込みのトレーシングに加え、OpenAI の評価・ファインチューニング・蒸留ツールのスイートを活用できます。

## インストール

```bash
pip install openai-agents
```

## Hello World のコード例

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