---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉は多義的です。注意すべき主なコンテキストには次の 2 つのクラスがあります:

1. コード内でローカルに利用できるコンテキスト: これはツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2.  LLM が利用できるコンテキスト: これは応答を生成するときに  LLM が参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。仕組みは次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的なパターンとして dataclass や Pydantic オブジェクトを使います。  
2. そのオブジェクトを各種 run メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。  

**最重要ポイント**: あるエージェント実行において、エージェント・ツール関数・ライフサイクルフックなどはすべて同じ *型* のコンテキストを使用しなければなりません。

コンテキストは次の用途に利用できます:

- 実行に関するコンテキストデータ（例: ユーザー名 / UID やその他のユーザー情報）
- 依存関係（例: ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "Note"
    コンテキストオブジェクトは **LLM に送信されません**。これは純粋にローカルのオブジェクトであり、読み書きやメソッド呼び出しを自由に行えます。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使えます。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツール実装はコンテキストを読み取ります。  
3. エージェントにジェネリック型 `UserInfo` を指定し、型チェッカーでの誤り（異なるコンテキスト型を取るツールを渡した場合など）を防止します。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

 LLM が呼び出されると、  LLM が参照できるデータは会話履歴のみです。したがって、新しいデータを  LLM に渡したい場合は、その履歴に含める形で提供する必要があります。方法はいくつかあります:

1. Agent の `instructions` に追加する  
   これは「システムプロンプト」または「デベロッパーメッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でも構いません。たとえばユーザー名や現在の日付など、常に有用な情報を渡す一般的な方法です。  

2. `Runner.run` を呼び出す際の `input` に追加する  
   これは `instructions` に追加する方法に似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上でより下位のメッセージとして扱えます。  

3. 関数ツールを介して公開する  
   これはオンデマンドのコンテキストに便利です。  LLM が必要と判断したときにツールを呼び出してデータを取得できます。  

4. retrieval または Web 検索を利用する  
   retrieval はファイルやデータベースから関連データを取得し、Web 検索は Web から情報を取得できる特殊なツールです。関連するコンテキストデータで応答をグラウンディングするのに役立ちます。