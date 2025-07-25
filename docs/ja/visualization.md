---
search:
  exclude: true
---
# エージェント可視化

エージェント可視化を使用すると、 ** Graphviz ** を用いてエージェントとその関係を構造的に図示できます。これは、アプリケーション内でエージェント、ツール、ハンドオフがどのように相互作用するかを理解するのに役立ちます。

## インストール

オプションの `viz` 依存グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用してエージェントの可視化を生成できます。この関数は、有向グラフを作成し、次のように表現します:

- **エージェント** は黄色のボックス。
- **ツール** は緑色の楕円。
- **ハンドオフ** は一方のエージェントから別のエージェントへの有向エッジ。

### 使用例

```python
from agents import Agent, function_tool
from agents.extensions.visualization import draw_graph

@function_tool
def get_weather(city: str) -> str:
    return f"The weather in {city} is sunny."

spanish_agent = Agent(
    name="Spanish agent",
    instructions="You only speak Spanish.",
)

english_agent = Agent(
    name="English agent",
    instructions="You only speak English",
)

triage_agent = Agent(
    name="Triage agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[spanish_agent, english_agent],
    tools=[get_weather],
)

draw_graph(triage_agent)
```

![Agent Graph](../assets/images/graph.png)

これにより、 ** triage agent ** の構造とサブエージェントおよびツールへの接続を視覚的に表現したグラフが生成されます。


## 可視化の理解

生成されたグラフには次の要素が含まれます:

- エントリーポイントを示す **スタートノード** (`__start__`)。
- 黄色で塗りつぶされた **長方形** で表されるエージェント。
- 緑色で塗りつぶされた **楕円** で表されるツール。
- 相互作用を示す有向エッジ:
  - エージェント間のハンドオフを示す **実線矢印**。
  - ツール呼び出しを示す **点線矢印**。
- 実行が終了する場所を示す **エンドノード** (`__end__`)。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは、`draw_graph` はグラフをインラインで表示します。別ウィンドウで表示するには、次のように記述します:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは、`draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。