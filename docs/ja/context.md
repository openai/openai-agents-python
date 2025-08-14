---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。ここで扱う主なコンテキストは 2 つあります。

1. コードでローカルに利用可能なコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバック、ライフサイクルフックなどで必要となるデータや依存関係です。
2. LLM に対して利用可能なコンテキスト: 応答生成時に LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その内部の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。動作は次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的には dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッド（例: `Runner.run(..., **context=whatever**)`）に渡します。
3. すべてのツール呼び出しやライフサイクルフックなどに、`RunContextWrapper[T]` というラッパーオブジェクトが渡されます。ここで `T` はコンテキストオブジェクトの型を表し、`wrapper.context` からアクセスできます。

  **最も重要な点** は、特定のエージェント実行において、あらゆるエージェント、ツール関数、ライフサイクルなどが同一のコンテキストの型を使用しなければならないことです。

コンテキストは次のような用途に使えます。

-   実行のための文脈データ（例: ユーザー名 / uid などの ユーザー に関する情報）
-   依存関係（例: ロガーオブジェクト、データフェッチャーなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM に送信されません。これは純粋にローカルなオブジェクトであり、読み書きやメソッド呼び出しができます。

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
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取り、ツール実装はコンテキストから読み取ります。
3. エージェントにジェネリックな `UserInfo` を付与して、型チェッカーがエラーを検出できるようにします（例えば、異なるコンテキスト型を受け取るツールを渡そうとした場合）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出し、年齢を取得します。

## エージェント / LLM コンテキスト

LLM が呼び出される際、参照できるデータは会話履歴に含まれるもの だけ です。そのため、LLM に新しいデータを利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. Agent の `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的関数でもかまいません。常に有用な情報（例: ユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出すときに `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位に配置されるメッセージを使えます。
3. 関数ツール を介して公開します。これはオンデマンドのコンテキストに有用で、LLM が必要に応じてツールを呼び出し、そのデータを取得できます。
4. リトリーバルや Web 検索 を使用します。これらは、ファイルやデータベースから関連データを取得（リトリーバル）したり、Web から取得（Web 検索）したりできる特別なツールです。関連する文脈データに基づいて応答をグラウンディングするのに役立ちます。