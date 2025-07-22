---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという用語は多義的です。ここでは主に 2 つのコンテキスト クラスがあります。

1. コード内でローカルに利用できるコンテキスト: これはツール関数の実行時や `on_handoff` などのコールバック、ライフサイクル フック内で必要となるデータや依存関係です。  
2. LLM が利用できるコンテキスト: これは LLM が応答を生成する際に参照できるデータです。

## ローカル コンテキスト

ローカル コンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。基本的な流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを利用するパターンが多いです。  
2. そのオブジェクトを各種 `run` メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべてのツール呼び出しやライフサイクル フックなどには、`RunContextWrapper[T]` というラッパー オブジェクトが渡されます。ここで `T` はコンテキスト オブジェクトの型で、`wrapper.context` からアクセスできます。  

**最も重要な点**: ひとつのエージェント実行において、エージェント・ツール関数・ライフサイクル フックなどはすべて同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に利用できます。

-   実行に関するコンテキスト データ（例: ユーザー名 / UID などのユーザー情報）  
-   依存関係（例: ロガー オブジェクト、データフェッチャーなど）  
-   ヘルパー関数  

!!! danger "Note"

    コンテキスト オブジェクトは **LLM に送信されません**。あくまでローカル オブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキスト オブジェクトです。ここでは dataclass を使用していますが、任意の型を利用できます。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることが分かります。ツール実装はコンテキストからデータを読み取ります。  
3. エージェントにジェネリック型 `UserInfo` を指定し、型チェッカーがエラーを検出できるようにしています（例: 異なるコンテキスト型を取るツールを渡そうとした場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴からのみ取得されます。そのため、新しいデータを LLM に渡したい場合は、履歴に含める形で提供する必要があります。主な方法は以下のとおりです。

1. エージェントの `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。システム プロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。ユーザー名や現在の日付など、常に有用な情報を渡す際によく使われます。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上で下位に位置するメッセージを扱える点が異なります。  
3. 関数ツール経由で公開する。これはオンデマンドのコンテキストに適しており、LLM が必要になったタイミングでツールを呼び出してデータを取得できます。  
4. リトリーバルまたは Web 検索を使用する。これらはファイルやデータベース（リトリーバル）、あるいは Web（Web 検索）から関連データを取得できる特殊なツールです。回答を関連するコンテキスト データに「グラウンディング」するのに役立ちます。