---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉は多義的です。ここでは、考慮すべきコンテキストの主なクラスは 2 つあります。

1. あなたのコード内でローカルに利用できるコンテキスト: これは、ツール関数の実行時や `on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータと依存関係です。  
2.  LLM が利用できるコンテキスト: これは LLM が応答を生成するときに参照するデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中にある [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使用します。  
2. そのオブジェクトをさまざまな run メソッド (例: `Runner.run(..., **context=whatever**)`) に渡します。  
3. すべてのツール呼び出しやライフサイクルフックなどには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。  

**最も重要** な点は、特定のエージェント実行において、エージェント、ツール関数、ライフサイクルフックなどはすべて同じコンテキストの _型_ を使用しなければならないということです。

コンテキストは次のような用途で利用できます。

- 実行時のコンテキストデータ (例: ユーザー名 / uid やその他の user に関する情報)  
- 依存関係 (例: ロガーオブジェクト、データフェッチャーなど)  
- ヘルパー関数  

!!! danger "Note"

    コンテキストオブジェクトは **LLM に送信されません**。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから値を読み取ります。  
3. ジェネリック型として `UserInfo` をエージェントに指定することで、型チェッカーがエラーを検出できます (たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など)。  
4. コンテキストは `run` 関数に渡されます。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

 LLM が呼び出される際に参照できるデータは、会話履歴の内容だけです。そのため、新しいデータを LLM に渡したい場合は、そのデータを会話履歴に含める形で提供しなければなりません。これを実現する方法はいくつかあります。

1. エージェントの `instructions` に追加する。これは「システムプロンプト」または「デベロッパーメッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を返す動的な関数でもかまいません。ユーザー名や現在の日付など、常に役立つ情報を渡す際によく使われる手法です。  
2. `Runner.run` 関数を呼び出す際に `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 内でより下位のメッセージとして渡せます。  
3. 関数ツールを通じて公開する。これはオンデマンドのコンテキストに便利です ― LLM が必要だと判断したときにツールを呼び出してデータを取得できます。  
4. retrieval や Web 検索を使用する。retrieval はファイルやデータベースから関連データを取得し、Web 検索は Web から取得します。関連するコンテキストデータで応答をグラウンディングするのに役立ちます。