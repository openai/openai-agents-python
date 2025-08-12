---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここでは、気に掛けるべきコンテキストには大きく 2 つのクラスがあります。

1. コードでローカルに利用できるコンテキスト: これは、tool 関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフック内などで必要になるデータや依存関係です。  
2. LLM が利用できるコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の流れは次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンが多いです。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべての tool 呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` が渡されます。ここで `T` は作成したコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。  

 **最も重要なのは**、同一のエージェント実行内では、エージェント・tool 関数・ライフサイクルフックなどがすべて同じ _型_ のコンテキストを使う必要があるという点です。

コンテキストは次のような用途に利用できます。

- 実行のためのコンテキストデータ（例: username/uid などユーザーに関する情報）  
- 依存関係（例: logger オブジェクトやデータフェッチャーなど）  
- ヘルパー関数  

!!! danger "Note"

    コンテキストオブジェクトは **送信されません** LLM へ。これは純粋にローカルオブジェクトで、読み書きやメソッド呼び出しを自由に行えます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を利用できます。  
2. これは tool です。`RunContextWrapper[UserInfo]` を受け取り、その実装でコンテキストを読み取ります。  
3. ジェネリック型 `UserInfo` でエージェントをマークすることで、型チェッカーがエラーを検出できます（例えば、異なるコンテキスト型を受け取る tool を渡そうとした場合）。  
4. `run` 関数にコンテキストが渡されます。  
5. エージェントは tool を正しく呼び出し、年齢を取得します。  

## エージェント／LLM コンテキスト

LLM が呼び出されたとき、LLM が参照できる **唯一の** データは会話履歴に含まれるものだけです。そのため、新しいデータを LLM に利用させたい場合は、そのデータを履歴に含める形で提供する必要があります。主な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を返す動的な関数でもかまいません。たとえばユーザー名や現在の日付など、常に有用な情報を渡す一般的な方法です。  
2. `Runner.run` を呼び出す際の `input` に追加する。`instructions` と似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位メッセージとして渡せる点が異なります。  
3. function tools を介して公開する。これは *オンデマンド* のコンテキストに便利です。LLM が必要になったタイミングで tool を呼び出し、データを取得できます。  
4. retrieval や Web 検索を利用する。これらはファイルやデータベースから関連データを取得する retrieval、あるいは Web から取得する Web 検索といった特別なツールです。関連コンテキストデータで応答を「グラウンディング」するのに役立ちます。