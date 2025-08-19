---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべきコンテキストには主に次の 2 つの種類があります。

1. コードからローカルに利用できるコンテキスト: これは、ツール関数の実行時、`on_handoff` のようなコールバック中、ライフサイクルフックなどで必要になる可能性があるデータや依存関係です。
2. LLM に利用できるコンテキスト: これは、LLM が応答を生成する際に参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスとその中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンは dataclass や Pydantic オブジェクトを使うことです。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` 経由でアクセスできます。

 ** 最も重要 ** な点: あるエージェントの実行では、そのエージェント、ツール関数、ライフサイクルなどのすべてが、同じ種類（_type_）のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます。

-   実行に関する状況データ（例: ユーザー名 / uid や他の ユーザー 情報など）
-   依存関係（例: ロガーオブジェクト、データ取得用のコンポーネントなど）
-   ヘルパー関数

!!! danger "注意"

    コンテキストオブジェクトは LLM へは送信されません。読み書きやメソッド呼び出しが可能な純粋なローカルオブジェクトです。

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

1. これはコンテキストオブジェクトです。ここでは dataclass を使用していますが、任意の型を使用できます。
2. これはツールです。`RunContextWrapper[UserInfo]` を受け取ることがわかります。ツールの実装はコンテキストから読み取ります。
3. 型チェッカーがエラーを検知できるように（たとえば、異なるコンテキスト型を受け取るツールを渡そうとした場合など）、エージェントに総称型 `UserInfo` を付けます。
4. コンテキストは `run` 関数に渡されます。
5. エージェントはツールを正しく呼び出して年齢を取得します。

## エージェント / LLM のコンテキスト

LLM が呼び出されたとき、LLM が参照できるのは会話履歴のデータのみです。つまり、新しいデータを LLM に利用可能にしたい場合は、その履歴で参照できる形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」とも呼ばれます。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。常に有用な情報（例: ユーザーの名前や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出す際の `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統に従う](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) 上で、より下位のメッセージとして配置できます。
3. 関数ツール を通じて公開します。これはオンデマンドのコンテキストに有用です。LLM が必要なときにデータの必要性を判断し、ツールを呼び出してそのデータを取得できます。
4. リトリーバルや Web 検索 を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連する状況データで応答を「グラウンディング」するのに有用です。