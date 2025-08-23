---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべき主なコンテキストには次の 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティを通じて表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックにはラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

最も重要な点: 特定のエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関するコンテキストデータ（例: ユーザー名 / uid やその他のユーザー情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることが分かります。ツールの実装はコンテキストから読み取ります。
3. エージェントにジェネリック型 `UserInfo` を付けて、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されたとき、LLM が参照できるデータは会話履歴のものだけです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴で利用可能になるような方法で行う必要があります。いくつかの方法があります。

1. エージェントの `instructions` に追加します。これは "system prompt" または「開発者メッセージ」とも呼ばれます。system prompts は静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。これは常に有用な情報（例えば、ユーザー名や現在の日付）に一般的な手法です。
2. `Runner.run` 関数を呼び出す際に `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージにできます。
3. 関数ツールを通じて公開します。これはオンデマンドのコンテキストに便利です。LLM がいつデータを必要とするかを判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。これは、関連するコンテキストデータに基づいて応答を根拠付けるのに役立ちます。