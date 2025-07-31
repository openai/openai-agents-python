---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉は多義的です。気に掛けるべき主なコンテキストは 2 つあります。

1. ローカルでコードからアクセスできるコンテキスト: ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLM が利用できるコンテキスト: LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして、 dataclass や Pydantic オブジェクトを使います。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックに、 `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はあなたのコンテキストオブジェクトの型で、 `wrapper.context` からアクセスできます。  

最も重要なポイント: あるエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルフックは、同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行用の文脈データ（例: ユーザー名 / uid など、 user に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。純粋にローカルで読み書きやメソッド呼び出しを行うためのオブジェクトです。

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
2. これはツールです。 `RunContextWrapper[UserInfo]` を受け取っています。ツールの実装はコンテキストから値を読み取ります。  
3. エージェントにジェネリック型 `UserInfo` を付けて、型チェッカーでエラー（たとえば異なるコンテキスト型を取るツールを渡した場合など）を検出できるようにします。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出される際、 LLM が参照できるデータは会話履歴のみです。そのため、新しいデータを LLM に見せたい場合は、そのデータを会話履歴に含める形で提供しなければなりません。方法はいくつかあります。

1. Agent の `instructions` に追加する。これは「 system prompt 」や「 developer message 」とも呼ばれます。 system prompt は静的な文字列でも、 context を受け取って文字列を返す動的関数でも構いません。常に有用な情報（例: ユーザーの名前や現在の日付）を渡す場合によく使われる手法です。  
2. `Runner.run` を呼び出す際の `input` に追加する。 `instructions` と似ていますが、より低い [chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) でメッセージを渡せます。  
3. 関数ツールを通じて公開する。これはオンデマンドでのコンテキスト共有に便利です。 LLM が必要に応じてツールを呼び出し、データを取得できます。  
4. リトリーバルや Web 検索を使う。これらはファイルやデータベースから関連データを取得する（リトリーバル）、または Web から取得する（Web 検索）ための特殊なツールです。関連する文脈データで応答をグラウンディングする際に役立ちます。