---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここでは主に次の 2 つのコンテキストがあります。

1. コードでローカルに利用可能なコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用可能なコンテキスト: 応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**))`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

 **最も重要** な点: あるエージェント実行において、すべてのエージェント、ツール関数、ライフサイクル等は同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行に関するコンテキストデータ（例: ユーザー名 / uid などのユーザー情報）
-   依存関係（例: ロガーオブジェクト、データ取得コンポーネントなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

```python
import asyncio
from dataclasses import dataclass

from agents import Agent, RunContextWrapper, Runner, function_tool

@dataclass
class UserInfo:  # (1)!
    name: str
    uid: int

@function_tool
async def fetch_user_age(wrapper: RunContextWrapper[UserInfo]) -> str:  # (2)!
    """Fetch the age of the user. Call this function to get user's age information."""
    return f"The user {wrapper.context.name} is 47 years old"

async def main():
    user_info = UserInfo(name="John", uid=123)

    agent = Agent[UserInfo](  # (3)!
        name="Assistant",
        tools=[fetch_user_age],
    )

    result = await Runner.run(  # (4)!
        starting_agent=agent,
        input="What is the age of the user?",
        context=user_info,
    )

    print(result.final_output)  # (5)!
    # The user John is 47 years old.

if __name__ == "__main__":
    asyncio.run(main())
```

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を利用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリック `UserInfo` を付与します（例えば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されるとき、参照できるデータは会話履歴のみです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴に含める形で提供する必要があります。方法はいくつかあります:

1. エージェントの `instructions` に追加します。これは「system prompt」または「開発者メッセージ」とも呼ばれます。system prompt は固定の文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザーの名前や現在の日付）に適した手法です。
2. `Runner.run` を呼ぶ際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)の下位にメッセージを配置できます。
3. 関数ツールで公開します。これは _オンデマンド_ のコンテキストに役立ちます。LLM が必要なときにデータ取得のためにツールを呼び出せます。
4. retrieval または Web 検索を使用します。これらは、ファイルやデータベース（retrieval）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答を「グラウンディング」するのに有用です。