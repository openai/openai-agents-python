---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語には複数の意味があります。ここでは、意識するべきコンテキストの主なクラスは 2 つあります。

1. コード内でローカルに利用できるコンテキスト: ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLMs が利用できるコンテキスト: 応答を生成するときに LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンがよく見られます。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., context=whatever)`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。  

**最も重要** なのは、1 回のエージェント実行においては、すべてのエージェント、ツール関数、ライフサイクルフックが同じ _型_ のコンテキストを使用しなければならない点です。

コンテキストは次のような用途で利用できます。

-   実行に関するコンテキストデータ（例: ユーザー名や UID などのユーザー情報）  
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）  
-   ヘルパー関数  

!!! danger "注意"

    コンテキストオブジェクトは **LLM には送信されません**。あくまでローカルオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを読み取ります。  
3. 型チェッカーでエラーを捕捉できるよう、エージェントにジェネリック型 `UserInfo` を指定しています（例として、異なるコンテキスト型を期待するツールを渡した場合など）。  
4. `run` 関数にコンテキストを渡しています。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント／ LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴に含まれるもの **のみ** です。そのため、新しいデータを LLM に提供したい場合は、そのデータが履歴に含まれるようにする必要があります。代表的な方法は次のとおりです。

1. Agent の `instructions` に追加する  
   - これは「system prompt」や「developer message」とも呼ばれます。system prompt は静的な文字列にも、コンテキストを受け取って文字列を返す動的な関数にもできます。たとえばユーザー名や現在の日付など、常に有用な情報を渡す際によく使われます。  
2. `Runner.run` を呼び出す際に `input` に追加する  
   - `instructions` と似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でより低いレベルのメッセージを渡したい場合に便利です。  
3. function tools を介して公開する  
   - これはオンデマンドコンテキストに適しています。LLM が必要に応じてツールを呼び出し、データを取得できます。  
4. retrieval や Web 検索を利用する  
   - retrieval はファイルやデータベースから、Web 検索はウェブ上から関連データを取得できる特殊なツールです。応答を適切なコンテキストに基づいて「グラウンディング」するのに役立ちます。