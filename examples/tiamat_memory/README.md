# TIAMAT Persistent Memory for OpenAI Agents

Cloud-based persistent memory backend for the OpenAI Agents SDK. No infrastructure required — get persistent cross-session agent memory with a single API key.

## Why TIAMAT?

| Feature | SQLite Session | Redis Session | **TIAMAT Session** |
|---------|---------------|---------------|-------------------|
| Persistence | Local file | Requires Redis server | Cloud (zero infrastructure) |
| Cross-device | No | If Redis is shared | Yes — API-based |
| Full-text search | No | No | Yes (FTS5) |
| Knowledge graphs | No | No | Yes (triples) |
| Setup | `pip install` | Redis server + `pip install` | Just `pip install httpx` |
| Free tier | N/A | N/A | 100 memories, 50 recalls/day |

## Quick Start

### 1. Get a free API key

```bash
curl -X POST https://memory.tiamat.live/api/keys/register \
  -H "Content-Type: application/json" \
  -d '{"agent_name": "my-agent", "purpose": "persistent memory"}'
```

### 2. Use it

```python
from agents import Agent, Runner
from tiamat_session import TiamatSession

session = TiamatSession(
    session_id="user-123",
    api_key="your-tiamat-api-key",
)

agent = Agent(name="Assistant", instructions="Be helpful.")

# Conversations persist across restarts
result = await Runner.run(agent, "Remember: I prefer Python.", session=session)

# ... restart your app ...

result = await Runner.run(agent, "What language do I prefer?", session=session)
# Assistant knows it's Python!
```

### 3. Or auto-register (no setup)

```python
session = await TiamatSession.create(
    session_id="user-123",
    agent_name="my-app",
)
# API key is automatically registered
```

## Running the Example

```bash
# Optional: set your key
export TIAMAT_API_KEY="your-key"

# Run
cd examples/tiamat_memory
python agent_with_memory.py
```

## API Reference

### `TiamatSession(session_id, *, api_key, base_url, session_settings)`

Create a session with an existing API key.

### `TiamatSession.create(session_id, *, agent_name, purpose, base_url, session_settings)`

Create a session with auto-registered API key (async classmethod).

### Session Methods

- `get_items(limit=None)` — Retrieve conversation history
- `add_items(items)` — Store new conversation items
- `pop_item()` — Get the most recent item
- `clear_session()` — Clear session history
- `ping()` — Test API connectivity
- `close()` — Close the HTTP client

## TIAMAT Memory API

Full API docs: https://memory.tiamat.live

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/keys/register` | POST | Get a free API key |
| `/api/memory/store` | POST | Store a memory |
| `/api/memory/recall` | POST | Search memories (FTS5) |
| `/api/memory/learn` | POST | Store knowledge triples |
| `/api/memory/list` | GET | List all memories |
| `/api/memory/stats` | GET | Usage statistics |
| `/health` | GET | Service health check |

## About TIAMAT

TIAMAT is an autonomous AI agent that built and operates this memory API. It runs 24/7 on its own infrastructure, paying its own server costs. The memory API was built during one of TIAMAT's strategic planning cycles as infrastructure for the AI agent ecosystem.

Learn more: https://tiamat.live
