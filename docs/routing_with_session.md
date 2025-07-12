# Routing with Session Memory

This guide explains how to implement a multilingual agent routing system using the new `Session` memory API in the `openai-agents-sdk`. It extends the classic handoff/routing pattern by preserving full conversation history between turns using `SQLiteSession`, enabling more natural and stateful interactions across agents.

---

## ✨ What This Example Does

This example demonstrates:

* **Language-based routing:** A triage agent detects the language and hands off to a language-specific agent (French, Spanish, English).
* **Session memory:** Using `SQLiteSession` to automatically track and persist conversation history.
* **Streaming responses:** The `Runner.run_streamed` method is used to display live assistant output.
* **Clean multi-turn loop:** User inputs are appended automatically to the session without manual input list handling.

---

## 🔧 How It Works

### 1. Define Your Agents

```python
french_agent = Agent(name="french_agent", instructions="You only speak French")
spanish_agent = Agent(name="spanish_agent", instructions="You only speak Spanish")
english_agent = Agent(name="english_agent", instructions="You only speak English")

triage_agent = Agent(
    name="triage_agent",
    instructions="Handoff to the appropriate agent based on the language of the request.",
    handoffs=[french_agent, spanish_agent, english_agent],
)
```

### 2. Create a `Session`

```python
conversation_id = str(uuid.uuid4().hex[:16])
session = SQLiteSession(conversation_id)
```

The `SQLiteSession` automatically saves and restores the full conversation history in a local file.

### 3. Stream Each Turn

```python
result = Runner.run_streamed(
    agent,
    input=msg,
    session=session,
)
```

This executes a streaming run with session memory. You only need to pass the latest user message, and the session provides the full context.

### 4. Print the Streamed Response

```python
async for event in result.stream_events():
    if isinstance(event.data, ResponseTextDeltaEvent):
        print(event.data.delta, end="", flush=True)
```

Each delta token is streamed to the console for a live typing effect.

### 5. Update Agent and Prompt for Next Turn

```python
msg = input("Enter a message: ")
agent = result.current_agent
```

The new `current_agent` is used for the next turn, allowing the system to maintain routing logic automatically.

---

## 🎓 Benefits of Using Session Memory

* **Automatic context handling:** You no longer need to manage `input` history lists manually.
* **Persistent conversations:** Sessions can be saved to disk and resumed across runs.
* **Cleaner logic:** Simplifies your multi-turn agent loops.
* **Better handoff support:** Maintains full continuity even when agents are changed mid-conversation.

---

## 🚀 Run the Example

### Requirements

* `openai-agents-sdk` with `SQLiteSession` support (install from main branch):

```bash
pip install git+https://github.com/openai/openai-agents-python@main
```

### Run

```bash
python examples/routing_with_session.py
```

### Sample Interaction

```text
Hi! We speak French, Spanish and English. How can I help?
> Hola, ¿cómo estás?

(Spanish agent responds)

Enter a message:
> Je veux apprendre le français.

(French agent responds)
```

---

## 📊 When to Use This Pattern

* Multilingual or multi-domain bots
* Context-sensitive routing
* Escalation flows between support agents
* Custom memory-backed agentic systems

---

## 🖐 Final Notes

This pattern highlights the power of `Session` memory in building modular, stateful agents. It removes boilerplate and supports more scalable multi-turn conversations with routing and handoffs.

For more on sessions, see: [Sessions Documentation](../docs/sessions.md)
