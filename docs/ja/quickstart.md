---
search:
  exclude: true
---
# クイックスタート

## プロジェクトと仮想環境の作成

これは一度だけ実行すれば十分です。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 仮想環境の有効化

新しいターミナルセッションを開始するたびに実行してください。

```bash
source .venv/bin/activate
```

### Agents SDK のインストール

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API キーの設定

まだお持ちでない場合は、OpenAI API キーを作成するために [こちらの手順](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key) に従ってください。

```bash
export OPENAI_API_KEY=sk-...
```

## 最初のエージェントの作成

エージェントは instructions、名前、および特定のモデルなどの任意の設定で定義します。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 最初のエージェントの実行

[`Runner`][agents.run.Runner] を使用してエージェントを実行し、[`RunResult`][agents.result.RunResult] を取得します。

```python
import asyncio
from agents import Agent, Runner

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)

async def main():
    result = await Runner.run(agent, "When did the Roman Empire fall?")
    print(result.final_output)

if __name__ == "__main__":
    asyncio.run(main())
```

2 回目のターンでは、`result.to_input_list()` を `Runner.run(...)` に戻して渡すか、[session](sessions/index.md) をアタッチするか、`conversation_id` / `previous_response_id` で OpenAI のサーバー管理状態を再利用できます。[running agents](running_agents.md) ガイドでは、これらのアプローチを比較しています。

次の目安を使ってください。

| 望んでいること | まず使うもの |
| --- | --- |
| 完全な手動制御とプロバイダー非依存の履歴 | `result.to_input_list()` |
| SDK に履歴の読み込みと保存を任せる | [`session=...`](sessions/index.md) |
| OpenAI 管理のサーバー側継続 | `previous_response_id` または `conversation_id` |

トレードオフと正確な動作については、[Running agents](running_agents.md#choose-a-memory-strategy) を参照してください。

タスクが主にプロンプト、ツール、会話状態で完結する場合は、プレーンな `Agent` と `Runner` を使用してください。エージェントが分離されたワークスペース内の実ファイルを検査または変更する必要がある場合は、[Sandbox agents quickstart](sandbox_agents.md) に進んでください。

## エージェントへのツール付与

エージェントに、情報を調べたりアクションを実行したりするためのツールを与えることができます。

```python
import asyncio
from agents import Agent, Runner, function_tool


@function_tool
def history_fun_fact() -> str:
    """Return a short history fact."""
    return "Sharks are older than trees."


agent = Agent(
    name="History Tutor",
    instructions="Answer history questions clearly. Use history_fun_fact when it helps.",
    tools=[history_fun_fact],
)


async def main():
    result = await Runner.run(
        agent,
        "Tell me something surprising about ancient life on Earth.",
    )
    print(result.final_output)


if __name__ == "__main__":
    asyncio.run(main())
```

## 追加エージェント

マルチエージェントパターンを選ぶ前に、最終回答を誰が担当するかを決めてください。

-   **ハンドオフ**: そのターンの該当部分では、専門エージェントが会話を引き継ぎます。
-   **Agents as tools**: オーケストレーターが制御を維持し、専門エージェントをツールとして呼び出します。

このクイックスタートでは、最初の例として最短であるため **ハンドオフ** を続けて扱います。マネージャースタイルのパターンについては、[Agent orchestration](multi_agent.md) と [Tools: agents as tools](tools.md#agents-as-tools) を参照してください。

追加のエージェントも同じ方法で定義できます。`handoff_description` は、いつ委譲するかについてルーティングエージェントに追加コンテキストを与えます。

```python
from agents import Agent

history_tutor_agent = Agent(
    name="History Tutor",
    handoff_description="Specialist agent for historical questions",
    instructions="You answer history questions clearly and concisely.",
)

math_tutor_agent = Agent(
    name="Math Tutor",
    handoff_description="Specialist agent for math questions",
    instructions="You explain math step by step and include worked examples.",
)
```

## ハンドオフの定義

エージェントでは、タスク解決中に選択可能な送信先ハンドオフオプションの一覧を定義できます。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## エージェントオーケストレーションの実行

ランナーは、個々のエージェント実行、ハンドオフ、ツール呼び出しを処理します。

```python
import asyncio
from agents import Runner


async def main():
    result = await Runner.run(
        triage_agent,
        "Who was the first president of the United States?",
    )
    print(result.final_output)
    print(f"Answered by: {result.last_agent.name}")


if __name__ == "__main__":
    asyncio.run(main())
```

## 参照コード例

リポジトリには、同じ主要パターンの完全なスクリプトが含まれています。

-   最初の実行向け: [`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)
-   関数ツール向け: [`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)
-   マルチエージェントルーティング向け: [`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)

## トレースの確認

エージェント実行中に何が起きたかを確認するには、[OpenAI ダッシュボードの Trace viewer](https://platform.openai.com/traces) に移動して、エージェント実行のトレースを表示してください。

## 次のステップ

より複雑なエージェントフローの構築方法を学びます。

-   [Agents](agents.md) の設定方法を学ぶ。
-   [running agents](running_agents.md) と [sessions](sessions/index.md) を学ぶ。
-   作業を実際のワークスペース内で行うべき場合は [Sandbox agents](sandbox_agents.md) を学ぶ。
-   [tools](tools.md)、[guardrails](guardrails.md)、[models](models/index.md) を学ぶ。