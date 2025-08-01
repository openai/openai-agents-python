---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉には複数の意味があります。ここでは主に 2 種類のコンテキストについて説明します。

1. コード内でローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係。
2. LLM が利用できるコンテキスト: LLM が応答を生成するときに参照できるデータ。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使用します。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要なポイント: あるエージェントの 1 回の実行において、エージェント、ツール関数、ライフサイクルフックなどはすべて同じ _型_ のコンテキストを使用する必要があります。

コンテキストは次のような用途に利用できます。

-   実行に関するデータ（例: ユーザー名 / uid やその他ユーザー情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。純粋にローカルで読み書きやメソッド呼び出しを行うためのオブジェクトです。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を利用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。
3. エージェントにジェネリック型 `UserInfo` を指定しているため、型チェッカーがエラーを検出できます（例: 異なるコンテキスト型を受け取るツールを渡した場合など）。
4. `run` 関数にコンテキストを渡しています。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出される際、LLM が参照できるデータは会話履歴のみです。したがって、新しいデータを LLM に提供したい場合は、そのデータが履歴に含まれるようにする必要があります。主な方法は次のとおりです。

1. エージェントの `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を返す動的な関数でもかまいません。ユーザー名や現在の日付など、常に有用な情報を渡す場合によく使われます。
2. `Runner.run` を呼び出すときの `input` に追加する。`instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でより下位のメッセージとして扱われます。
3. 関数ツール経由で公開する。これはオンデマンドのコンテキストに便利です。LLM が必要に応じてツールを呼び出し、そのデータを取得できます。
4. リトリーバルや Web 検索を使う。これらはファイルやデータベースから関連データを取得する（リトリーバル）、あるいは Web から取得する（Web 検索）特殊なツールです。回答を適切なコンテキストデータに「基づかせる」ために有用です。