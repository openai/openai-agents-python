---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語には複数の意味があります。ここでは主に次の 2 つの種類のコンテキストを扱います。

1. コードからローカルに参照できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に提供されるコンテキスト: これは、応答を生成する際に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティによって表現されます。動作の流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどには、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` を通じてアクセスできます。

最も **重要** な点: 特定のエージェント実行に関わるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます:

-   実行時の状況データ（例: ユーザー名/ UID やユーザーに関するその他の情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装はコンテキストから読み取ります。
3. 型チェッカーでエラーを検出できるように、エージェントにジェネリック `UserInfo` を付与しています（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント/ LLM のコンテキスト

LLM が呼び出されるとき、参照できるのは会話履歴に含まれるデータのみです。つまり、LLM に新しいデータを利用させたい場合は、その履歴に含められる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。常に有用な情報（例: ユーザーの名前や現在の日付）に適した手法です。
2. `Runner.run` を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)の下位にメッセージを配置できます。
3. 関数ツール経由で公開します。これはオンデマンドのコンテキストに有用で、LLM が必要になったタイミングでツールを呼び出してデータを取得できます。
4. リトリーバルまたは Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）、あるいは Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに基づいて応答に根拠付けを行うのに有用です。