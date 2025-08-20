---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。ここでは主に次の 2 種類のコンテキストがあります。

1. コードでローカルに利用可能なコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係。
2. LLM に利用可能なコンテキスト: 応答を生成する際に LLM が参照できるデータ。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。`T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要な注意点: 特定のエージェント実行において、すべてのエージェント、ツール関数、ライフサイクルなどは同じ「型」のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行のための状況データ（例: ユーザー名 / uid など ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データ取得器など）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装ではコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるように、エージェントにジェネリクス `UserInfo` を指定します（例: 異なるコンテキスト型を取るツールを渡そうとした場合）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴のものだけです。したがって、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できるようにする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは "system prompt"（または "developer message"）とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザーの名前や現在の日付）に適した方法です。
2. `Runner.run` を呼び出すときに `input` に追加します。これは `instructions` の戦術に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に配置されるメッセージを持てます。
3. 関数ツールとして公開します。これはオンデマンドのコンテキストに有効です。LLM は必要に応じてデータが必要かどうかを判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連する状況データに基づいて応答をグラウンディングするのに有用です。