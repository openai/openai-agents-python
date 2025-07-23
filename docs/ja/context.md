---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉には複数の意味があります。ここでは主に次の 2 つのカテゴリーについて扱います。

1. コード内でローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係。
2. LLM が参照できるコンテキスト: LLM がレスポンスを生成する際に参照するデータ。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。基本的な流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンが多いです。
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。`T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要なポイントは、1 回のエージェント実行内で使用されるすべてのエージェント、ツール関数、ライフサイクルフックが同じ「型」のコンテキストを共有しなければならないということです。

コンテキストを利用して、次のようなことが可能です。

-   実行に関するデータ（例: ユーザー名 / UID などユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。これは完全にローカルなオブジェクトであり、読み書きやメソッド呼び出しのみが行えます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、どのような型でも構いません。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを読み取っています。
3. ジェネリック型 `UserInfo` をエージェントに指定し、型チェッカーがエラーを検出できるようにします（例: 異なるコンテキスト型を要求するツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

 LLM が呼び出される際、**唯一** 参照できるデータは会話履歴に含まれるものだけです。つまり、新しいデータを LLM に利用させたい場合は、会話履歴にそのデータを含める必要があります。主な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」あるいは「developer message」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。ユーザー名や現在の日付など常に役立つ情報に適した方法です。
2. `Runner.run` を呼び出す際に `input` に追加する。`instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージとして挿入できます。
3. 関数ツール経由で公開する。これはオンデマンドのコンテキストに便利で、 LLM が必要なタイミングでツールを呼び出してデータを取得できます。
4. リトリーバルや Web 検索を使用する。これはファイルやデータベースから関連データを取得する（リトリーバル）あるいは Web から取得する（Web 検索）特殊なツールです。関連コンテキストに基づいてレスポンスを「グラウンディング」する際に有用です。