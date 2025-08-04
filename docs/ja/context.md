---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここでは主に 2 つのコンテキスト クラスが存在します。

1. コード側でローカルに利用できるコンテキスト: ツール関数の実行時や `on_handoff` などのコールバック、ライフサイクル フック内で必要となるデータや依存関係を指します。  
2. LLM が利用できるコンテキスト: 応答を生成する際に LLM が参照できるデータを指します。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作の流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを用いるパターンが多いです。  
2. そのオブジェクトを各種 `run` メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。  
3. すべてのツール呼び出しやライフサイクル フックには `RunContextWrapper[T]` 型のラッパー オブジェクトが渡されます。ここで `T` はコンテキスト オブジェクトの型で、`wrapper.context` からアクセスできます。

**最重要ポイント**: 1 回のエージェント実行において、エージェント本体・ツール関数・ライフサイクル フックなどはすべて同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途で利用できます。

-   実行に関連するデータ（例: username/uid などユーザーに関する情報）  
-   依存オブジェクト（例: ロガーやデータフェッチャーなど）  
-   ヘルパー関数  

!!! danger "Note"

    コンテキスト オブジェクトは LLM へは **送信されません**。ローカル専用オブジェクトとして読み書きやメソッド呼び出しを行います。

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

1. これはコンテキスト オブジェクトです。ここでは dataclass を使用していますが、任意の型で構いません。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーがエラーを検出できます（例えば異なるコンテキスト型を取るツールを渡そうとした場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントは正しくツールを呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出される際、LLM が参照できるデータは **会話履歴だけ** です。そのため、新しいデータを LLM が利用できるようにするには、履歴に追加する形で渡す必要があります。主な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。システム プロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。常に有用な情報（例: ユーザー名や現在の日付）を渡す際によく用いられます。  
2. `Runner.run` を呼び出す際の `input` に追加する。`instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できる点が異なります。  
3. 関数ツール経由で公開する。これはオンデマンド コンテキストに適しており、LLM が必要に応じてツールを呼び出してデータを取得できます。  
4. Retrieval や Web 検索を使う。これらはファイルやデータベース（retrieval）あるいは Web（Web 検索）から関連データを取得できる特別なツールです。応答を適切なコンテキスト データに基づいて「グラウンディング」する際に有効です。