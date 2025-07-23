---
search:
  exclude: true
---
# コンテキスト管理

コンテキスト (context) という言葉には複数の意味があります。主に次の 2 つのコンテキストを扱います。

1. コード側でローカルに利用できるコンテキスト: これはツール関数実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLM が利用できるコンテキスト: これはレスポンス生成時に LLM が参照できるデータです。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うことが多いです。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever** )`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要なポイント** : 1 つのエージェント実行につき、エージェント、ツール関数、ライフサイクルフックなどはすべて同じ _型_ のコンテキストを使用する必要があります。

コンテキストの主な用途は次のとおりです。

-   実行に関するデータ (例: ユーザー名 / uid などの ユーザー 情報)
-   依存関係 (例: ロガーオブジェクト、データフェッチャーなど)
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。あくまでローカルで読み書きやメソッド呼び出しを行うためのオブジェクトです。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型で構いません。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーがエラーを検出できます（異なるコンテキスト型のツールを渡そうとした場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出される際、LLM が参照できるデータは会話履歴のみです。そのため、新しいデータを LLM に渡したい場合は、会話履歴に含める形で提供する必要があります。主な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「システムプロンプト」や「デベロッパーメッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。ユーザー名や現在の日付など、常に有用な情報を渡す場合によく使われます。  
2. `Runner.run` を呼び出す際の `input` に追加する。`instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上でより下位のメッセージとして渡せます。  
3. function tools を通じて公開する。オンデマンドでコンテキストを取得させたい場合に便利で、LLM が必要に応じてツールを呼び出してデータを取得します。  
4. retrieval や web search を使用する。retrieval はファイルやデータベースから関連データを取得し、web search は Web から取得します。これにより、回答を関連コンテキストで「グラウンディング」できます。