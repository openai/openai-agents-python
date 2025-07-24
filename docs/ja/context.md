---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。主に気に掛けるべきコンテキストには 2 つのクラスがあります。

1. コードからローカルに参照できるコンテキスト: これはツール関数の実行時や `on_handoff` などのコールバック、ライフサイクルフック内で必要となるデータや依存関係です。  
2. LLM から参照できるコンテキスト: これは LLM が応答を生成する際に閲覧できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとして dataclass や Pydantic オブジェクトを使います。  
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。  
3. すべてのツール呼び出しやライフサイクルフックにはラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。  

最も重要なポイント: 同じエージェント実行内のすべてのエージェント、ツール関数、ライフサイクルフックは、同一 _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に使えます。

-   実行に関するコンテキストデータ（例: username/uid やその他の user 情報など）  
-   依存関係（例: logger オブジェクト、データフェッチャーなど）  
-   ヘルパー関数  

!!! danger "Note"

    コンテキストオブジェクトは **LLM には送信されません**。ローカル専用のオブジェクトであり、読み書きやメソッド呼び出しが自由に行えます。

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

1. これがコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を利用できます。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. エージェントにジェネリック型 `UserInfo` を付与することで型チェッカーが誤りを検出できます（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント / LLM コンテキスト

LLM が呼び出されるとき、LLM が閲覧できるデータは会話履歴に含まれるもの **のみ** です。したがって、新しいデータを LLM に参照させたい場合は、会話履歴に組み込む形で提供する必要があります。代表的な方法は次のとおりです。

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。system prompt は静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でもかまいません。たとえば user の名前や現在の日付など、常に有用な情報を渡す際によく使われます。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位メッセージとして配置できる点が異なります。  
3. 関数ツール経由で公開する。オンデマンドでコンテキストを取得させたい場合に便利です。LLM が必要に応じてツールを呼び出し、データを取得できます。  
4. retrieval や web search を使用する。これらはファイルやデータベースから関連データを取得する retrieval、または Web から情報を取得する web search といった特殊ツールです。関連コンテキストに基づいた「根拠づけ」を行いたい場合に有効です。