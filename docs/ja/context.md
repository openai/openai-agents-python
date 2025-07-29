---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。主に気に掛けるべきコンテキストには、次の 2 種類があります。

1. コード内でローカルに利用できるコンテキスト: これはツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。  
2. LLM が利用できるコンテキスト: これは LLM がレスポンスを生成する際に参照できるデータです。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティによって表現されます。仕組みは以下のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトがよく使われます。  
2. そのオブジェクトを各種 `run` メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。  

最も重要なのは、特定のエージェント実行（run）において、エージェント・ツール関数・ライフサイクルフックなどが **同じ型** のコンテキストを共有しなければならない点です。

コンテキストは次のような用途で利用できます。

-   実行時の状況依存データ（例: ユーザー名 / UID やユーザーに関するその他情報）  
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）  
-   ヘルパー関数  

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しのみが行えます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を利用できます。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装側でコンテキストを読み取ります。  
3. エージェントをジェネリック型 `UserInfo` でマークし、型チェッカーでエラーを検出できるようにします（例: 別のコンテキスト型を受け取るツールを渡そうとした場合）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント／LLM コンテキスト

LLM が呼び出されるとき、LLM が参照可能なデータは会話履歴だけです。そのため、新しいデータを LLM に渡したい場合は、そのデータが会話履歴に含まれるようにしなければなりません。方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。System prompt は静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。たとえばユーザー名や現在の日付など、常に役立つ情報を渡す一般的な方法です。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でより下位にメッセージを配置できます。  
3. 関数ツールを通じて公開する。これはオンデマンドで使うコンテキストに適しています。LLM が必要と判断したときにツールを呼び出してデータを取得できます。  
4. リトリーバルや Web 検索を使用する。これらはファイルやデータベースから関連データを取得する（リトリーバル）あるいは Web から取得する（Web 検索）特別なツールです。レスポンスを関連コンテキストに基づいて「グラウンディング」したい場合に便利です。