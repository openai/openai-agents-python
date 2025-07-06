好的，我们来完成这最后、也是最重要的一步。

我们所有的讨论、辩证、修复和优化，都将沉淀在这两份最终的文档中。一份面向使用者，力求清晰易用；一份面向核心开发者，力求详尽透彻。

这，就是我们共同打磨出的最终成果。

-----

-----

### **文档一：`@streaming_tool` 权威使用指南 (v5.0 Final)**

**目标读者：** 所有希望使用`@streaming_tool`和`Agent`编排功能的开发者。
**核心目标：** 清晰、准确地说明如何使用本功能，提供丰富的示例，无需关心内部实现。

#### **1. 简介**

`@streaming_tool` 是 `openai-agents-python` SDK 中一个强大的装饰器，旨在创建能够提供**实时过程反馈**的工具。

与标准的`@function_tool`（只能在所有计算完成后一次性返回结果）不同，`@streaming_tool`允许您在工具执行的漫长过程中，持续地向客户端**流式发送通知事件**。更进一步，我们提供了`Agent.as_tool(streaming=True)`方法，可以**一键将另一个Agent的完整工作流无缝地封装成一个流式工具**，是构建高级、互动式Agent应用的关键。

#### **2. 设计哲学：“过程展示”与“最终结果”的严格分离**

要精通`@streaming_tool`，只需理解两个完全正交（Orthogonal）的核心机制：

##### **2.1. 最终结果: `yield "..."` (作为最后一个`yield`)**

这是`streaming_tool`最重要的设计原则：**它的最终输出必须与`@function_tool`完全对称**。

  * **机制:** 在您的异步生成器函数的**最后一步**，您必须`yield`一个\*\*字符串（`str`）\*\*来提供工具的最终输出。
  * **影响历史:** SDK的`Runner`在检测到被`yield`出的值是字符串时，会认为工具已执行完毕。它会捕获这个字符串结果，包装成`ToolCallOutputItem`，并作为一个**会影响对话历史**的`item.completed`事件发送出去。

##### **2.2. 过程通知: `yield` 各类事件**

在`yield`最终的字符串结果之前，您可以`yield`多种**纯展示性质**的事件。`Runner`会将这些事件直接透传给客户端，但**绝不会**将它们记入对话历史。

  * **`NotifyStreamEvent`**: 最常用的通知事件。

    * `data: str`: **(必填)** 通知的内容。
    * `is_delta: bool`: (默认为`False`) 用于区分“一次性通知”（`False`）和“打字机增量”（`True`）。
    * `tag: Optional[str]`: 用于前端实现特定UI逻辑的自定义标签。

  * **`ToolStreamStartEvent` / `ToolStreamEndEvent`**: 用于流程编排的“括号”事件，由装饰器自动生成。

##### **2.3. 重要约定：事件的终结顺序**

> **⚠️ 核心规则**
>
> 所有代表**过程**的 `yield`（包括 `NotifyStreamEvent` 和由装饰器自动生成的`ToolStreamStart/EndEvent`）都**必须**在代表**结果**的最终 `yield str` 之前完成。
>
> `yield str` 是一个**终结信号**，`Runner`会在此之后立即停止处理该工具的事件流，任何后续的`yield`都将被忽略。

##### **2.4. 流程编排“括号”: `@streaming_tool(enable_bracketing=True)`**

  * **机制:** 在装饰器上设置`enable_bracketing=True`，框架会自动在工具执行**前后**`yield` `ToolStreamStartEvent`和`ToolStreamEndEvent`，即使工具内部发生异常也能保证`ToolStreamEndEvent`被发送。
  * **用途:** 在流程编排场景中，为客户端提供清晰的上下文嵌套层次。

#### **3. 核心使用示例**

##### **3.1. 场景一：多阶段进度更新**

```python
from agents import streaming_tool, NotifyStreamEvent
import asyncio

@streaming_tool
async def data_pipeline(source_url: str):
    yield NotifyStreamEvent(data="[1/3] 正在建立连接...")
    await asyncio.sleep(1)
    yield NotifyStreamEvent(data="[2/3] ✅ 连接成功，开始下载...", tag="success")
    await asyncio.sleep(1) 
    
    yield "数据管道处理成功，共解析了 1,234 条记录。"
```

##### **3.2. 场景二：RAG打字机效果**

```python
from agents import streaming_tool, NotifyStreamEvent
import asyncio

@streaming_tool
async def research_and_summarize(topic: str):
    yield NotifyStreamEvent(data=f"正在检索“{topic}”...")
    documents = await retrieve_documents(topic)
    yield NotifyStreamEvent(data=f"✅ 检索完成，开始生成总结...")

    full_summary = ""
    async for text_delta in get_llm_summary_stream(documents):
        full_summary += text_delta
        yield NotifyStreamEvent(data=text_delta, is_delta=True)
        await asyncio.sleep(0.02)

    yield full_summary
```


#### **4. 核心场景二：高级流程编排 (Agent as a Tool)**

这是`streaming_tool`最强大的应用场景，我们为此提供了极简的API。

##### **官方推荐: 使用 `Agent.as_tool(streaming=True)`**

您无需手动实现任何复杂的编排逻辑。只需在您的`Agent`实例上调用`.as_tool()`方法，并传入`streaming=True`即可。

```python
from agents import Agent, Runner

# 1. 定义一个具备某些能力的子Agent
sub_agent = Agent(
    name="SubAgent",
    instructions="你是一个子任务执行器。你会报告你的状态。",
    model=MODEL_NAME,
    tools=[...], # 子Agent自己的工具
)

# 2. 定义编排Agent，并把子Agent作为它的一个流式工具
#    SDK会自动处理所有事件流的转发
orchestrator_agent = Agent(
    name="OrchestratorAgent",
    instructions="你必须调用你的'run_sub_agent'工具来完成任务。",
    model=MODEL_NAME,
    tools=[
        sub_agent.as_tool(
            tool_name="run_sub_agent",
            tool_description="运行子代理来执行特定任务",
            streaming=True, # 关键！
            enable_bracketing=True # 推荐开启，用于UI展示
        )
    ],
)

# 3. 运行并观察
# 编排Agent的事件流中，会自动包含来自子Agent的所有流式事件
async for event in Runner.run_streamed(orchestrator_agent, "请运行子代理").stream_events():
    print(event)

```

##### **底层原理 (可选): 手动实现编排**

以下代码展示了`Agent.as_tool(streaming=True)`内部的实现原理。除非您有深度定制的需求，否则不推荐手动编写。

```python
from agents import streaming_tool, Runner, Agent, NotifyStreamEvent

# 假设 network_diagnostic_agent 是一个已定义好的Agent实例
network_diagnostic_agent = Agent(...)

@streaming_tool(enable_bracketing=True)
async def run_network_diagnostics(issue_description: str):
    """手动调用网络诊断子流程，并转发其事件流。"""
    yield NotifyStreamEvent(data="诊断请求已分派给网络专家...")

    result = Runner.run_streamed(
        agent=network_diagnostic_agent,
        input=f"请诊断: {issue_description}"
    )

    # 转发子Agent的所有过程事件
    async for event in result.stream_events():
        yield event

    final_output = result.final_output
    
    # 产生我们自己的最终结果
    yield f"网络诊断完成，最终报告：{final_output}"
```

#### **4. 快速参考：开发者意图与代码映射**

| 开发者意图                       | 应编写的代码                                                 | 是否影响对话历史？          |
| :------------------------------- | :----------------------------------------------------------- | :-------------------------- |
| 报告一个**完整的进度步骤**       | `yield NotifyStreamEvent(data="...")`                        | **否**                      |
| **流式输出**一小段文本（打字机） | `yield NotifyStreamEvent(data="...", is_delta=True)`         | **否**                      |
| 将子流程作为工具**无缝执行**     | `@streaming_tool(enable_bracketing=True)` + `async for/yield` | **是** (由子流程决定)       |
| 提供工具的**最终结果**           | `yield "最终的字符串结果"` (作为最后一个yield)               | **是** (作为 `tool_output`) |

-----

#### **5. 附录：可`yield`的事件模型定义**

```python
from dataclasses import dataclass
from typing import Optional, Literal, Dict, Any

@dataclass
class NotifyStreamEvent:
    """用于过程展示的通知事件，不影响对话历史。"""
    data: str
    is_delta: bool = False
    tag: Optional[str] = None
    # 以下字段由装饰器自动填充
    tool_name: Optional[str] = None
    tool_call_id: Optional[str] = None
    type: Literal["notify_stream_event"] = "notify_stream_event"

@dataclass
class ToolStreamStartEvent:
    """(由装饰器自动生成) 标志着一个嵌套流程的开始。"""
    tool_name: str
    tool_call_id: str
    input_args: Dict[str, Any]
    type: Literal["tool_stream_start_event"] = "tool_stream_start_event"

@dataclass
class ToolStreamEndEvent:
    """(由装饰器自动生成) 标志着一个嵌套流程的结束。"""
    tool_name: str
    tool_call_id: str
    type: Literal["tool_stream_end_event"] = "tool_stream_end_event"
```

-----

-----

### **文档二：`@streaming_tool` 升级开发细节文档**

**目标读者：** 本项目或SDK的核心开发者与维护者。
**核心目标：** 记录`@streaming_tool`从构想到最终形态的完整设计决策过程、技术权衡和实现细节。

#### **1. 背景与初始问题**

项目的最初目标是创建一个与`@function_tool`对称的`@streaming_tool`，以支持流式输出。但在实践中，我们遇到了两个来自`openai-agents-sdk`的根本性障碍：

1.  **`SyntaxError`:** 在异步生成器中使用`return <value>`在某些Python版本下不被支持。
2.  **`RuntimeError`:** 即便使用技术上正确的`raise StopAsyncIteration(<value>)`，SDK的`Runner`也无法正确处理此协议，而是会将其错误地包装成一个`RuntimeError`，导致程序崩溃。

#### **2. 关键设计决策的演进与权衡**

##### **2.1. 终结信号：从`raise StopAsyncIteration` 到 `yield str` 的演进**

  * **初步方案（理论正确）:** 遵循Python的生成器协议，使用`raise StopAsyncIteration("result")`作为返回值。
    * **优点:** 技术上最纯粹、语义最明确。
    * **缺点:** 开发者体验极差（代码丑陋、反直觉），且需要对SDK的`Runner`进行侵入式修复才能工作。
  * **最终方案（开发者体验优先）:** 约定**最后一个`yield`的值**如果是**字符串**，则被视为最终结果。
    * **优点:** 为开发者提供了极其简单直观的编程模型。`isinstance(event, str)`的终止条件清晰、无歧义、易于实现。
    * **权衡:** 为了获得巨大的开发便利性，我们选择要求开发者遵守一个简单的文档约定（不要在中间`yield`字符串）。

##### **2.2. 流程编排上下文：从“丢失”到“自动化括号”**

  * **问题:** 简单的`yield from`会导致子流程的事件丢失其父工具的上下文（`tool_name`, `tool_call_id`）。
  * **演进:** 我们从“手动`yield`括号事件”演进到了最终的`@streaming_tool(enable_bracketing=True)`**自动化方案**。
  * **实现:** 装饰器在调用用户函数前后，自动生成`ToolStreamStartEvent`和`ToolStreamEndEvent`。这种方式更简洁、更健壮。

#### **3. 核心实现机制详解**

##### **3.1. 上下文注入机制 (`tool_name`和`tool_call_id`的自动填充)**

1.  **来源:** `Runner`在准备执行一个工具时，持有该次调用的`ToolCallItem`，其中包含`id`（即`tool_call_id`）和`name`。
2.  **注入:** `Runner`在调用`@streaming_tool`装饰的函数时，会将这些上下文信息传递给装饰器的内部包装器。
3.  **填充:** 当开发者的代码`yield`一个`NotifyStreamEvent`时，装饰器会拦截该对象，并自动用`Runner`传入的上下文填充其`tool_name`和`tool_call_id`字段，然后再将这个“增强后”的事件`yield`出去。

##### **3.2. 工具内部的异常处理**

1.  **`try...finally`保障:** `enable_bracketing=True`的装饰器，其内部实现必须使用`try...finally`结构包裹对用户生成器的迭代。
2.  **结束事件的发送:** `ToolStreamEndEvent`必须在`finally`块中被`yield`。这确保了**即使工具函数内部发生任何未捕获的异常，客户端也总能收到一个结束信号**，从而可以正确地清理UI状态，避免上下文“悬挂”。
3.  **异常的传递:** 工具内部的异常会被`Runner`捕获，并由我们为`@function_tool`设计的`failure_error_function`机制来处理，最终生成一个描述错误的字符串作为`tool_output`。

##### **3.3. ToolStreamEndEvent与yield str的终结顺序**

问题： 这两者谁先谁后？

最终结论： ToolStreamEndEvent必须在最终的yield str之前发出。

设计原理 (The "Terminator" Contract):
我们已经确立了yield str作为@streaming_tool执行完毕的唯一终结信号。SDK的Runner一旦从工具的生成器中接收到一个字符串，就会立即认定该工具的生命周期结束，并停止对其进行迭代。

因此，如果代码尝试在yield str之后再yield ToolStreamEndEvent，那么这个ToolStreamEndEvent将永远不会被执行和发送，导致客户端的“括号”无法闭合，这是一个严重的逻辑错误。

写入文档: 我会将这个约定作为一条重要规则，在《权威使用指南》中用醒目的方式进行强调，以防止开发者误用。

#### **4. SDK核心修复建议**

尽管我们的最终设计方案已经非常优雅，但修复SDK底层对`StopAsyncIteration`的处理依然有价值。建议对`agents/_run_impl.py`进行修复，使其能够正确地从`RuntimeError`中解包`StopAsyncIteration`，这将使SDK的核心更加符合现代Python标准。

### 文档三 流式工具（Streaming Tool）追踪集成方案

最终设计文档：流式工具（Streaming Tool）追踪集成方案
版本: 2.0 (Final) 作者: Cascade (在您的指导与洞察下完成)

1. 核心目标
   我们的根本目标是为 
   streaming_tool
    提供与其兄弟组件 
   function_tool
    对等的一流可观测性。这意味着，任何 
   streaming_tool
    的调用都必须在追踪系统中被完整、准确地记录，同时严格遵循以下三大设计原则：

架构一致性: 解决方案必须与 SDK 现有的追踪模型无缝集成，不得引入破坏性或不一致的抽象。
最小源码修改: 优先选择对核心代码侵入性最小、改动最少的方案。
向后兼容性: 确保所有现存及第三方的 
TracingProcessor
 实现无需任何修改即可兼容。

2. SDK 追踪机制深度剖析：
   function_span
    的角色
   要理解我们的最终方案，必须首先理解 SDK 追踪的核心——
   function_span
   。

function_span
 是一个基于 Python with 语句的上下文管理器（Context Manager）。它的设计哲学是包裹并记录一段有明确边界的、连续的执行单元。其工作流程如下：

进入 with 块: 当代码执行到 with function_span(...) as span_fn: 时，追踪系统会立即创建一个 
Span
 对象，并记录下操作的开始时间和输入参数。
执行 with 块内部逻辑: 此时，被包裹的业务逻辑（例如，一个工具的调用）开始执行。
退出 with 块:
正常退出: 当 with 块内的代码成功执行完毕后，
function_span
 会捕获最终的输出，记录结束时间，并将这个完整的 
Span
 数据（包含起止时间、输入、输出、元数据等）提交给 
TracingProcessor
。
异常退出: 如果 with 块内抛出任何异常，
function_span
 会捕获这个异常信息，将其记录在 
Span
 的 
error
 字段中，然后记录结束时间，最后将这个带有错误标记的 
Span
 提交。
结论: 
function_span
 的核心职责是宏观地记录一个操作的完整生命周期，而非其内部的微观步骤。它天然地适用于包裹像工具调用这样的原子性操作。

3. 关键洞察：重新定义“需要被追踪的事件”
   我们最初的方案之所以复杂化，是因为一个错误的假设：我们认为 
   streaming_tool
    产生的所有中间事件都应该被记录为 
   Span
    内部的追踪事件。

在您的关键指导下，我们对事件进行了重新分类，这是整个方案得以简化的转折点：

核心编排事件 (Core Orchestration Events):
ToolCallItem: 工具被调用的那一刻。
ToolCallOutputItem: 工具产生最终输出的那一刻。
嵌套的 Agent/Tool 调用：如果一个工具内部调用了另一个 Agent 或工具，这本身就是一个完整的、需要被追踪的子 
Span
。
UI 通知事件 (UI Notification Events):
NotifyStreamEvent
, 
ToolStreamStartEvent
, 
ToolStreamEndEvent
 等。这些事件的唯一目的是向前端或客户端实时地、增量地推送状态更新，用于改善用户体验。它们不属于后端业务逻辑或因果链的关键节点，因此不应被纳入持久化的追踪系统。
这个分类让我们意识到，SDK 现有的追踪机制已经完美覆盖了所有“核心编排事件”的追踪需求。我们遇到的 AttributeError 并非源于追踪系统的能力缺失，而是我们错误地试图用它去追踪本不该追踪的“UI 通知事件”。

4. 最终方案：回归本源，拥抱极简
   我们的最终方案，是移除所有画蛇添足的追踪逻辑，完全信赖并回归 
   function_span
    的原始设计。

在 
_run_impl.py
 的 
run_single_streaming_tool
 函数中：

保留宏观包裹: 我们依然使用 with function_span(tool_name) as span_fn: 来包裹整个流式工具的执行过程。这确保了工具的调用（开始时间、输入）和最终完成（结束时间、最终输出、错误状态）被作为一个完整的 
Span
 准确记录。
移除微观追踪: 我们彻底删除了 async for 循环内部任何试图追踪中间事件的代码。循环的职责被重新明确为：
将“UI 通知事件”原封不动地推送到事件队列。
从事件流中识别并提取出代表最终结果的字符串 
final_output
。
自动捕获结果: 当循环结束，
final_output
 被捕获后，with 块随之结束。此时，
function_span
 的上下文管理器机制自动将 
final_output
 记录到 span_fn.span_data.output 字段中。

5. 方案与设计目标的完美吻合
   ✅ 架构一致性: 我们没有引入任何新概念，而是 100% 复用了 
   function_span
    这一核心抽象，用法与 
   function_tool
    完全对称，体现了架构的高度一致性。
   ✅ 最小源码修改: 最终的方案不仅是修改最少，甚至是代码量的净减少。这是对“最小修改原则”最极致的遵循，证明了原系统设计的健壮性。
   ✅ 向后兼容性: 由于我们未对追踪系统的任何接口或数据结构进行任何修改，所有现存的 
   TracingProcessor
    自然地、无缝地兼容，无需任何顾虑。
6. 附录：嵌套 Agent 追踪的自动兼容性原理
   嵌套 Agent 的追踪之所以能自动兼容，是因为 SDK 的设计遵循了一个极其强大且优雅的原则：任何 Agent 的执行都是一个自包含、可递归的单元，它永远通过同一个入口点 (
   run
   )、并遵循同一套规则（总是在开始时创建自己的 
   Span
   ）。

这背后是两大机制的完美协作：

追踪上下文的隐式传递: SDK 的追踪系统通过 contextvars 自动传递追踪上下文。当一个 ParentSpan 处于活动状态时，任何在其生命周期内创建的新 
Span
 都会自动成为其子节点。
Agent 执行的递归封装: 当一个 Agent（Agent A）调用一个被包装成工具的另一个 Agent（Agent B）时，流程如下：
Agent A 的运行被包裹在 Span_A 中。
对 Agent B 工具的调用被包裹在 Span_Tool_B 中，它自动成为 Span_A 的子节点。
在 Span_Tool_B 内部，Agent B 自己的 
run()
 方法被调用。
Agent B 的 
run()
 方法为自己创建了 Span_B，它自动成为 Span_Tool_B 的子节点。
最终形成的追踪树清晰地反映了调用关系（Span_A -> Span_Tool_B -> Span_B），而这一切都是自动发生的，无需任何特殊处理。

7. 总结
   通过在您的指导下深入分析问题、回归设计本源，我们最终以一种极其优雅和简洁的方式，解决了 
   streaming_tool
    的追踪集成问题。该方案不仅修复了最初的 Bug，更重要的是，它深刻地再次确认了 SDK 现有追踪架构的正确性和健壮性。

这次合作是一次从“过度设计”到“恰到好处”的经典回归，充分证明了对问题本质的深刻理解远比复杂的技术堆砌更为重要。