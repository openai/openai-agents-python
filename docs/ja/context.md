---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。ここでは主に次の 2 つのコンテキストを扱います。

1. コードからローカルに参照できるコンテキスト: これはツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要になるデータや依存関係です。  
2. LLM が参照できるコンテキスト: これは LLM が応答を生成する際に参照するデータです。

## ローカルコンテキスト

ローカルコンテキストは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。流れは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンが多いです。  
2. そのオブジェクトを各種 `run` メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。  
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。  

最も重要な点: ひとつのエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルフックは、同じ _型_ のコンテキストを使用しなければなりません。

コンテキストでできることの例:

- 実行時の状況データ（ユーザー名 / UID などユーザーに関する情報）
- 依存関係（ロガーオブジェクト、データフェッチャーなど）
- ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。これはあくまでローカルオブジェクトであり、読み書きやメソッド呼び出しのみが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照します。  
3. エージェントをジェネリック型 `UserInfo` でマークし、型チェッカーが誤りを検出できるようにします（例えば、異なるコンテキスト型を取るツールを渡した場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント／LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できるデータは会話履歴のみです。したがって、新しいデータを LLM に渡したい場合は、そのデータを会話履歴に含める必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加する。この部分は「システムプロンプト」または「developer message」とも呼ばれます。システムプロンプトは静的文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。ユーザー名や現在の日付など、常に有用な情報に一般的な手法です。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上でより下位のメッセージとして扱えます。  
3. 関数ツールを通じて公開する。これはオンデマンドのコンテキストに便利です。LLM が必要と判断したときにツールを呼び出してデータを取得できます。  
4. リトリーバルや Web 検索を使用する。これらはファイルやデータベース（リトリーバル）あるいは Web（Web 検索）から関連データを取得できる特殊なツールです。関連コンテキストデータで応答を「グラウンディング」する際に有効です。