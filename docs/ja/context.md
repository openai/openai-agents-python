---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。重要になるコンテキストには、主に次の 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLMs に提供されるコンテキスト: 応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティによって表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。よくあるパターンは dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出し、ライフサイクルフックなどにはラッパーオブジェクト `RunContextWrapper[T]` が渡されます。`T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

** 最重要 ** な注意点: 特定のエージェント実行においては、そのエージェント、ツール関数、ライフサイクルなどのすべてが同じ「型」のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行のためのコンテキストデータ（例: ユーザー名 / uid など、ユーザーに関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取っていることがわかります。ツールの実装ではコンテキストから読み取ります。
3. 型チェッカーがエラーを検出できるよう、エージェントにジェネリックの `UserInfo` を指定します（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. `run` 関数にコンテキストを渡します。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴にあるものだけです。したがって、新しいデータを LLM に利用可能にするには、そのデータを会話履歴で参照できる形にする必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは "system prompt" や "developer message" とも呼ばれます。System prompt は静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` と似た手法ですが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツールで公開します。これはオンデマンドのコンテキストに有用です。LLM が必要なタイミングを判断し、ツールを呼び出してそのデータを取得できます。
4. 検索（retrieval）や Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（retrieval）したり、Web から取得（Web 検索）したりできる特別なツールです。関連するコンテキストデータに基づいて応答をグラウンディングするのに有用です。