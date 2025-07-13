---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉には複数の意味があります。ここでは主に 2 つのコンテキストについて説明します。

1. コード内でローカルに利用できるコンテキスト: ツール関数の実行時や `on_handoff` などのコールバック、ライフサイクルフックで必要となるデータや依存関係です。  
2. LLM が参照できるコンテキスト: LLM がレスポンスを生成する際に見えるデータです。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして dataclass や Pydantic オブジェクトを使用します。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever** )`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最重要ポイント**: あるエージェントの実行において、エージェント・ツール関数・ライフサイクルフックなどはすべて同じ _型_ のコンテキストを使用しなければなりません。

コンテキストでは次のような用途が考えられます。

-   実行に関するデータ（例: ユーザー名 / uid やその他のユーザー情報）
-   依存オブジェクト（例: ロガー、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM には送信されません。あくまでローカルのオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
    return f"User {wrapper.context.name} is 47 years old"

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使っていますが、任意の型を使用できます。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック `UserInfo` を付与することで、型チェッカーが誤りを検出できます（たとえば別のコンテキスト型を受け取るツールを渡した場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴に含まれるものだけです。したがって、新しいデータを LLM に渡したい場合は、そのデータを履歴に含める形で提供する必要があります。方法はいくつかあります。

1. Agent の `instructions` に追加する。いわゆる「system prompt」や「developer message」と呼ばれるものです。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的な関数でも構いません。ユーザー名や現在の日付など、常に有用な情報を渡す際によく使われます。  
2. `Runner.run` 呼び出し時の `input` に追加する。`instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位レイヤーにメッセージを配置できます。  
3. 関数ツール経由で公開する。オンデマンドで取得するコンテキストに適しており、LLM が必要に応じてツールを呼び出してデータを取得します。  
4. retrieval や web search を使う。これらは特別なツールで、ファイルやデータベースから関連データを取得する（retrieval）、もしくは Web から取得する（web search）ことができます。レスポンスを関連コンテキストで「グラウンディング」するのに有効です。