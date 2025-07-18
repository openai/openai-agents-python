---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという言葉には複数の意味があります。ここでは主に次の 2 つのコンテキストを扱います:

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時や `on_handoff` などのコールバック、ライフサイクルフック内で必要となるデータや依存関係です。  
2. LLM から参照できるコンテキスト: これは、LLM が応答を生成するときに見るデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。流れは次のとおりです:

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使うパターンが多いです。  
2. そのオブジェクトを各種 run メソッド (例: `Runner.run(..., **context=whatever**)`) に渡します。  
3. すべてのツール呼び出しやライフサイクルフックでは、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。`T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要な点**: 1 回のエージェント実行において、エージェント・ツール関数・ライフサイクルフックなどはすべて同じ _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に利用できます:

-   実行のためのコンテキストデータ (例: ユーザー名や UID など ユーザー に関する情報)  
-   依存関係 (例: ロガーオブジェクトやデータフェッチャーなど)  
-   ヘルパー関数  

!!! danger "Note"
    コンテキストオブジェクトは **LLM には送信されません**。あくまでローカルオブジェクトであり、読み書きやメソッド呼び出しが可能です。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、どのような型でも構いません。  
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを参照しています。  
3. ジェネリック型 `UserInfo` をエージェントに指定することで、型チェッカーが誤り (異なるコンテキスト型を取るツールを渡すなど) を検出できます。  
4. `run` 関数にコンテキストを渡します。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。  

## エージェント／LLM コンテキスト

LLM が呼び出されるとき、LLM が参照できる **唯一** のデータは会話履歴だけです。したがって、新しいデータを LLM に与えたい場合は、そのデータを履歴に含める方法を取る必要があります。主な方法は次のとおりです:

1. Agent の `instructions` に追加する。これは「system prompt」や「developer message」とも呼ばれます。`instructions` は静的な文字列でも、コンテキストを受け取って文字列を返す動的関数でも構いません。常に役立つ情報 (例: ユーザー名や現在の日付) を渡す一般的な手法です。  
2. `Runner.run` を呼び出す際に `input` に追加する。この方法は `instructions` と似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できる点が異なります。  
3. 関数ツールを通じて公開する。これは _オンデマンド_ コンテキストに適しており、LLM が必要になったタイミングでツールを呼び出してデータを取得できます。  
4. Retrieval や Web 検索を使用する。これらはファイルやデータベースから関連データを取得する (retrieval) または Web から取得する (Web 検索) 特殊なツールです。関連するコンテキストデータで回答をグラウンディングするのに有効です。