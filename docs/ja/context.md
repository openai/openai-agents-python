---
search:
  exclude: true
---
# コンテキスト管理

コンテキストは多義的な用語です。考慮すべき主なコンテキストには、次の 2 つのクラスがあります。

1. コードからローカルに利用できるコンテキスト: ツール関数の実行時、`on_handoff` のようなコールバックやライフサイクルフックの最中に必要となるデータや依存関係です。
2. LLM に利用可能なコンテキスト: 応答を生成するときに LLM が参照できるデータです。

## ローカルコンテキスト

これは [`RunContextWrapper`][agents.run_context.RunContextWrapper] クラスと、その中の [`context`][agents.run_context.RunContextWrapper.context] プロパティで表現されます。仕組みは次のとおりです。

1. 任意の Python オブジェクトを作成します。一般的なパターンとしては dataclass や Pydantic オブジェクトを使います。
2. そのオブジェクトを各種の実行メソッドに渡します（例: `Runner.run(..., **context=whatever**)`）。
3. すべてのツール呼び出しやライフサイクルフックなどには、ラッパーオブジェクト `RunContextWrapper[T]` が渡されます。ここで `T` はコンテキストオブジェクトの型で、`wrapper.context` からアクセスできます。

 **最も重要** な注意点: あるエージェント実行におけるすべてのエージェント、ツール関数、ライフサイクルなどは、同じ型のコンテキストを使用する必要があります。

コンテキストは次のような用途に使えます:

-   実行時の状況データ（例: ユーザー名 / uid など、ユーザーに関する情報）
-   依存関係（例: logger オブジェクト、データ取得ロジックなど）
-   ヘルパー関数

!!! danger "Note"

    コンテキストオブジェクトは LLM には **送信されません**。これは純粋にローカルなオブジェクトで、読み書きやメソッド呼び出しが可能です。

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
3. エージェントにはジェネリック `UserInfo` を付与し、型チェッカーがエラーを検出できるようにします（たとえば、異なるコンテキスト型を取るツールを渡そうとした場合など）。
4. コンテキストは `run` 関数に渡されます。
5. エージェントは正しくツールを呼び出し、年齢を取得します。

## エージェント/LLM コンテキスト

LLM が呼び出されると、参照できるデータは会話履歴にあるもの **のみ** です。したがって、LLM に新しいデータを利用可能にしたい場合は、その履歴に含める形で提供する必要があります。方法はいくつかあります。

1. エージェントの `instructions` に追加します。これは「システムプロンプト」または「開発者メッセージ」としても知られています。システムプロンプトは静的な文字列でも、コンテキストを受け取って文字列を出力する動的な関数でもかまいません。常に有用な情報（たとえばユーザー名や現在の日付）に適した一般的な手法です。
2. `Runner.run` 関数を呼び出すときの `input` に追加します。これは `instructions` の手法に似ていますが、[指揮系統](https://cdn.openai.com/spec/model-spec-2024-05-08.html#follow-the-chain-of-command) の下位にメッセージを配置できます。
3. 関数ツールを通じて公開します。これはオンデマンドのコンテキストに有用で、LLM が必要なときにデータの取得ツールを呼び出せます。
4. リトリーバルや Web 検索を使用します。これらは、ファイルやデータベース（リトリーバル）または Web（Web 検索）から関連データを取得できる特別なツールです。関連するコンテキストデータに応じて応答を「グラウンディング（根拠付け）」するのに役立ちます。