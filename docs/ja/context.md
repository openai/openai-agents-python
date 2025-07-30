---
search:
  exclude: true
---
# コンテキスト管理

コンテキスト (context) という言葉には複数の意味があります。ここでは、主に気に掛けるべきコンテキストには 2 つの大きなクラスがあります。

1. コード内でローカルに利用できるコンテキスト: これはツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLM が利用できるコンテキスト: これはレスポンス生成時に LLM が参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては、 dataclass や Pydantic オブジェクトを使用します。  
2. そのオブジェクトを各種 run メソッドに渡します (例: `Runner.run(..., **context=whatever**)`)。  
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、 `wrapper.context` からアクセスできます。  

**最も重要なポイント**: 1 回のエージェント実行において、エージェント・ツール関数・ライフサイクルフックなどは、必ず同じ型のコンテキストを共有する必要があります。

コンテキストは次のような用途に利用できます。

-   実行に関するデータ (例: ユーザー名 / UID などの ユーザー 情報)  
-   依存関係 (例: logger オブジェクトやデータフェッチャーなど)  
-   ヘルパー関数  

!!! danger "Note"
    コンテキストオブジェクトは **LLM に送信されません**。あくまでもローカルなオブジェクトであり、読み書きやメソッド呼び出しにのみ使用します。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型で構いません。  
2. これはツールです。 `RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック型 `UserInfo` を指定しているため、型チェッカーで (異なるコンテキスト型を受け取るツールを渡そうとした場合など) エラーを検出できます。  
4. `run` 関数にコンテキストを渡しています。  
5. エージェントはツールを正しく呼び出し、 age を取得します。  

## エージェント / LLM のコンテキスト

LLM が呼び出される際、 LLM が参照できるデータは会話履歴だけです。そのため、新しいデータを LLM に渡したい場合は、そのデータを会話履歴に組み込む必要があります。主な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。 system prompt は固定の文字列でも、コンテキストを受け取って文字列を返す動的関数でもかまいません。ユーザー名や現在の日付など、常に有用な情報に適した方法です。  
2. `Runner.run` を呼び出す際に `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位レベルにメッセージを配置できます。  
3. function tools を通じて公開する。これはオンデマンドのコンテキストに適しており、 LLM が必要になったタイミングでツールを呼び出してデータを取得できます。  
4. retrieval や web search を使用する。これらはファイルやデータベースから関連データを取得する (retrieval) 、あるいは Web から取得する (web search) 特別なツールです。関連するコンテキストデータでレスポンスを「グラウンディング」するのに適しています。