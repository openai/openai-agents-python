---
search:
  exclude: true
---
# コンテキスト管理

コンテキストという語は多義的です。関心を持つ可能性があるコンテキストには、大きく 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: ツール関数実行時、`on_handoff` などのコールバック、ライフサイクルフック内で必要になるデータや依存関係です。  
2. LLM から利用できるコンテキスト: LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスおよびその [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使用します。  
2. そのオブジェクトを各種 run メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。  
3. すべてのツール呼び出しやライフサイクルフックには `RunContextWrapper[T]` というラッパーオブジェクトが渡されます。`T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

**最も重要** な注意点: 同じエージェント実行内のすべてのエージェント、ツール関数、ライフサイクルフックは、同一 _型_ のコンテキストを使用しなければなりません。

コンテキストは次のような用途に利用できます。

-   実行に関するコンテキストデータ（例: ユーザー名 / uid など user に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは **LLM へ送信されません**。純粋にローカルで読み書きやメソッド呼び出しを行うためのオブジェクトです。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、実装内でコンテキストを読み取っています。  
3. エージェントをジェネリック型 `UserInfo` でマークすることで、型チェッカーが誤り（例: 異なるコンテキスト型のツールを渡した場合）を検出できます。  
4. `run` 関数にコンテキストを渡しています。  
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出される際、**唯一** 参照できるデータは会話履歴からのものです。そのため、新しいデータを LLM に利用させたい場合は、その履歴に組み込む形で提供しなければなりません。主な方法は次のとおりです。

1. エージェントの `instructions` に追加する。これは「system prompt」や「developer message」としても知られます。system prompt は静的文字列でも、コンテキストを受け取って文字列を出力する動的関数でも構いません。たとえば user の名前や現在の日付など、常に役立つ情報を提供する一般的な手法です。  
2. `Runner.run` を呼び出す際の `input` に追加する。この方法は `instructions` と似ていますが、[chain of command](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上でより下位のメッセージとして扱えます。  
3. function tools を通じて公開する。必要に応じて LLM がデータを取得できるオンデマンド型のコンテキストに適しています。  
4. リトリーバルや Web 検索を利用する。これらはファイルやデータベースから関連データを取得する（リトリーバル）、または Web から取得する（Web 検索）特殊なツールです。関連するコンテキストデータで応答を「グラウンディング」したい場合に有効です。