---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。考慮すべき主なコンテキストは次の 2 つです。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。
2. LLM に利用できるコンテキスト: 応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要** な点: あるエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同一のコンテキストの型を使用しなければなりません。

コンテキストは次の用途に使えます。

- 実行のためのコンテキストデータ（例: ユーザー名 / uid やユーザーに関するその他の情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に **送信されません**。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しができます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使えます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装がコンテキストから読み取っています。
3. エージェントにジェネリックの `UserInfo` を指定し、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/LLM のコンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴にあるもの **のみ** です。つまり、LLM に新しいデータを利用させたい場合は、その履歴で参照可能になるように提供しなければなりません。方法はいくつかあります。

1. Agent の `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは固定文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）に適した手法です。
2. `Runner.run` を呼び出すときの `input` に追加します。これは `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) においてより下位のメッセージにできます。
3. 関数ツールとして公開します。これはオンデマンドのコンテキストに有用で、LLM が必要だと判断したときにツールを呼び出してデータを取得できます。
4. リトリーバル (retrieval) や Web 検索を使用します。これらは、ファイルやデータベースから関連データを取得（リトリーバル）したり、Web から取得（Web 検索）したりできる特別なツールです。関連するコンテキストデータに基づいて応答をグラウンディングするのに有用です。