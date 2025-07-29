---
search:
  exclude: true
---
# エージェントの可視化

エージェントの可視化では、 **Graphviz** を使用してエージェントとそれらの関係を構造化されたグラフィカル表現として生成できます。これにより、アプリケーション内でエージェント、ツール、ハンドオフがどのように相互作用するかを理解しやすくなります。

## インストール

オプションの `viz` 依存関係グループをインストールします:

```bash
pip install "openai-agents[viz]"
```

## グラフの生成

`draw_graph` 関数を使用してエージェントの可視化を生成できます。この関数は次のような有向グラフを作成します:

- **エージェント** は黄色のボックスで表されます。  
- **ツール** は緑色の楕円で表されます。  
- **ハンドオフ** は一方のエージェントから別のエージェントへ向かう有向エッジとして示されます。

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

![エージェント グラフ](../assets/images/graph.png)

これにより、 **triage agent** の構造と、そのサブエージェントおよびツールとの接続を視覚的に示すグラフが生成されます。

## 可視化の理解

生成されたグラフには次の要素が含まれます:

- **start node** (`__start__`) がエントリーポイントを示します。  
- エージェントは黄色で塗りつぶされた長方形として表示されます。  
- ツールは緑色で塗りつぶされた楕円として表示されます。  
- 相互作用を示す有向エッジ:  
  - **Solid arrows** はエージェント間のハンドオフを示します。  
  - **Dotted arrows** はツール呼び出しを示します。  
- **end node** (`__end__`) が実行の終了地点を示します。

## グラフのカスタマイズ

### グラフの表示
デフォルトでは、 `draw_graph` はグラフをインラインで表示します。別ウィンドウで表示するには、次のように記述します:

```python
draw_graph(triage_agent).view()
```

### グラフの保存
デフォルトでは、 `draw_graph` はグラフをインラインで表示します。ファイルとして保存するには、ファイル名を指定します:

```python
draw_graph(triage_agent, filename="agent_graph")
```

これにより、作業ディレクトリに `agent_graph.png` が生成されます。