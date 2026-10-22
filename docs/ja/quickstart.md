---
search:
  exclude: true
---
# クイックスタート

## プロジェクトと仮想環境の作成 {#create-a-project-and-virtual-environment}

この作業は一度だけ行います。

```bash
mkdir my_project
cd my_project
python -m venv .venv
```

### 仮想環境の有効化 {#activate-the-virtual-environment}

新しいターミナルセッションを開始するたびに、この作業を行います。

macOS または Linux の場合:

```bash
source .venv/bin/activate
```

Windows の場合:

```cmd
.venv\Scripts\activate
```

### Agents SDK のインストール {#install-the-agents-sdk}

```bash
pip install openai-agents # or `uv add openai-agents`, etc
```

### OpenAI API キーの設定 {#set-an-openai-api-key}

キーをお持ちでない場合は、[こちらの手順](https://platform.openai.com/docs/quickstart#create-and-export-an-api-key)に従って OpenAI API キーを作成してください。

以下のコマンドは、現在のターミナルセッションにキーを設定します。

macOS または Linux の場合:

```bash
export OPENAI_API_KEY=sk-...
```

Windows PowerShell の場合:

```powershell
$env:OPENAI_API_KEY = "sk-..."
```

Windows コマンドプロンプトの場合:

```cmd
set "OPENAI_API_KEY=sk-..."
```

## 最初のエージェントの作成 {#create-your-first-agent}

エージェントは、instructions、名前、および特定のモデルなどのオプション設定で定義します。

```python
from agents import Agent

agent = Agent(
    name="History Tutor",
    instructions="You answer history questions clearly and concisely.",
)
```

## 最初のエージェントの実行 {#run-your-first-agent}

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

2 回目のターンでは、`result.to_input_list()` を `Runner.run(...)` に再度渡すか、[セッション](sessions/index.md)を関連付けるか、`conversation_id` / `previous_response_id` を使用して OpenAI のサーバー管理状態を再利用できます。[エージェントの実行](running_agents.md)ガイドでは、これらのアプローチを比較しています。

次の目安を参考にしてください:

| 目的 | 最初に使用するもの |
| --- | --- |
| 完全な手動制御とプロバイダーに依存しない履歴 | `result.to_input_list()` |
| SDK による履歴の読み込みと保存 | [`session=...`](sessions/index.md) |
| OpenAI が管理するサーバー側での継続 | `previous_response_id` または `conversation_id` |

トレードオフと正確な動作については、[エージェントの実行](running_agents.md#choose-a-memory-strategy)を参照してください。

タスクの中心がプロンプト、ツール、会話状態である場合は、通常の `Agent` と `Runner` を使用します。エージェントが隔離されたワークスペース内の実ファイルを調査または変更する必要がある場合は、[サンドボックスエージェントのクイックスタート](sandbox_agents.md)に進んでください。

## エージェントへのツールの追加 {#give-your-agent-tools}

エージェントにツールを追加し、情報の検索やアクションの実行を可能にできます。

```python
import asyncio
from agents import Agent, Runner
from agents.decorators import tool


@tool
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

## エージェントの追加 {#add-a-few-more-agents}

マルチエージェントパターンを選択する前に、最終的な回答を誰が担当するかを決めます:

-   **ハンドオフ**: そのターンの該当部分について、スペシャリストが会話を引き継ぎます。
-   **Agents as tools**: オーケストレーターが制御を維持し、スペシャリストをツールとして呼び出します。

このクイックスタートでは、最初の例として最も簡潔なため、引き続き **ハンドオフ** を使用します。マネージャー形式のパターンについては、[エージェントオーケストレーション](multi_agent.md)および[ツール: Agents as tools](tools.md#agents-as-tools)を参照してください。

追加のエージェントも同じ方法で定義できます。`handoff_description` は、どのタイミングで処理を委任するかについて、ルーティングエージェントに追加のコンテキストを提供します。

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

## ハンドオフの定義 {#define-your-handoffs}

エージェントには、タスクの解決中に選択できる送信先ハンドオフの選択肢一覧を定義できます。

```python
triage_agent = Agent(
    name="Triage Agent",
    instructions="Route each homework question to the right specialist.",
    handoffs=[history_tutor_agent, math_tutor_agent],
)
```

## エージェントオーケストレーションの実行 {#run-the-agent-orchestration}

Runner は、個々のエージェント、すべてのハンドオフ、およびすべてのツール呼び出しの実行を処理します。

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

## 参照コード例 {#reference-examples}

リポジトリには、同じ主要パターンを示す完全なスクリプトが含まれています:

-   最初の実行については、[`examples/basic/hello_world.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/hello_world.py)を参照してください。
-   関数ツールについては、[`examples/basic/tools.py`](https://github.com/openai/openai-agents-python/tree/main/examples/basic/tools.py)を参照してください。
-   マルチエージェントルーティングについては、[`examples/agent_patterns/routing.py`](https://github.com/openai/openai-agents-python/tree/main/examples/agent_patterns/routing.py)を参照してください。

## トレースの表示 {#view-your-traces}

エージェントの実行中に何が起きたかを確認するには、[OpenAI Dashboard の Trace viewer](https://platform.openai.com/traces)に移動して、エージェント実行のトレースを表示します。

## 次のステップ {#next-steps}

より複雑なエージェントフローを構築する方法を学びます:

-   [エージェント](agents.md)の設定方法を学びます。
-   [エージェントの実行](running_agents.md)と[セッション](sessions/index.md)について学びます。
-   実際のワークスペース内で作業を行う必要がある場合は、[サンドボックスエージェント](sandbox_agents.md)について学びます。
-   [ツール](tools.md)、[ガードレール](guardrails.md)、[モデル](models/index.md)について学びます。