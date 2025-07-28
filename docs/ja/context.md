---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。主に次の 2 種類のコンテキストがあります。

1. ローカルでコードから参照できるコンテキスト: ツール関数が実行される際や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLM が参照できるコンテキスト: レスポンスを生成するときに LLM が目にするデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表されます。動作の流れは以下のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンが多いです。  
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。  
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はあなたのコンテキストオブジェクト型で、 `wrapper.context` からアクセスできます。  

最も重要なポイント: 1 回のエージェント実行において、エージェント・ツール関数・ライフサイクルフックなどが使用するコンテキストは、必ず同じ “型” でなければなりません。

コンテキストでできることの例:

- 実行時のコンテキストデータ（例: ユーザー名 / UID など ユーザー に関する情報）  
- 依存関係（例: ロガーオブジェクト、データ取得用オブジェクトなど）  
- ヘルパー関数  

!!! danger "Note"

    コンテキストオブジェクトは LLM には送信されません。完全にローカルなオブジェクトであり、読み書きやメソッド呼び出しのみが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を利用できます。  
2. これはツールです。 `RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック型 `UserInfo` を指定することで、型チェッカーが誤りを検知できます（たとえば異なるコンテキスト型を取るツールを渡した場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出される際に参照できるデータは、会話履歴に含まれるものだけです。そのため、新しいデータを LLM に利用させたい場合は、会話履歴に載せる形で提供する必要があります。主な方法は次のとおりです。

1. エージェントの `instructions` に追加する。これは “system prompt” や “developer message” とも呼ばれます。`instructions` は静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。ユーザー名や現在の日付のように常に有用な情報に適しています。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command)内でより低い位置にメッセージを配置できます。  
3. function tools で公開する。オンデマンドのコンテキストに便利で、LLM が必要になったときにツールを呼び出してデータを取得させられます。  
4. Retrieval や Web 検索を使う。これらはファイルやデータベースから関連データを取得する retrieval、または Web から取得する Web 検索という特殊なツールです。関連するコンテキストデータに基づいて回答を “グラウンディング” したい場合に有用です。