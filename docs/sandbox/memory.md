# Agent memory

Memory lets future sandbox-agent runs learn from prior runs. It is separate from the SDK's conversational [`Session`](../sessions/index.md) memory, which stores message history. Memory distills lessons from prior runs into files in the sandbox workspace.

!!! warning "Beta feature"

    Sandbox agents are in beta. Expect details of the API, defaults, and supported capabilities to change before general availability, and expect more advanced features over time.

Memory can reduce three kinds of cost for future runs:

1. Agent cost: If the agent took a long time to complete a workflow, the next run should need less exploration. This can reduce token usage and time to completion.
2. User cost: If the user corrected the agent or expressed a preference, future runs can remember that feedback. This can reduce human intervention.
3. Context cost: If the agent completed a task before, and the user wants to build on that task, the user should not need to find the previous thread or re-type all the context. This makes task descriptions shorter.

See [examples/sandbox/memory.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory.py) for a complete two-run example that fixes a bug, generates memory, resumes a snapshot, and uses that memory in a follow-up verifier run. See [examples/sandbox/memory_multi_agent_multiturn.py](https://github.com/openai/openai-agents-python/blob/main/examples/sandbox/memory_multi_agent_multiturn.py) for a multi-turn, multi-agent example with separate memory layouts.

## Enable memory

Add `Memory()` as a capability to the sandbox agent.

```python
from pathlib import Path
import tempfile

from agents.sandbox import LocalSnapshotSpec, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

agent = SandboxAgent(
    name="Memory-enabled reviewer",
    instructions="Inspect the workspace and preserve useful lessons for follow-up runs.",
    capabilities=[Memory(), Filesystem(), Shell()],
)

with tempfile.TemporaryDirectory(prefix="sandbox-memory-example-") as snapshot_dir:
    sandbox = await client.create(
        manifest=manifest,
        snapshot=LocalSnapshotSpec(base_path=Path(snapshot_dir)),
    )
```

If read is enabled, `Memory()` requires `Shell()`, which lets the agent read and search memory files when the injected summary is not enough. When live memory update is enabled (by default), it also requires `Filesystem()`, which lets the agent update `memories/MEMORY.md` if the agent discovers stale memory or the user asks it to update memory.

By default, memory artifacts are stored in the sandbox workspace under `memories/`. To reuse them in a later run, preserve and reuse the whole configured memories directory by keeping the same live sandbox session or resuming from a persisted session state or snapshot; a fresh empty sandbox starts with empty memory.

`Memory()` enables both reading and generating memories. Use `Memory(generate=None)` for agents that should read memory but should not generate new memories: for example, an internal agent, subagent, checker, or one-off tool agent whose run doesn't add much signal. Use `Memory(read=None)` when the run should generate memory for later, but the user doesn't want the run to be influenced by existing memory.

## Read memory

Memory reads use progressive disclosure. At the start of a run, the SDK injects a small summary (`memory_summary.md`) of generally useful tips, user preferences, and available memories into the agent's developer prompt. This gives the agent enough context to decide whether prior work may be relevant.

When prior work looks relevant, the agent searches the configured memory index (`MEMORY.md` under `memories_dir`) for keywords from the current task. It opens the corresponding prior rollout summaries under the configured `rollout_summaries/` directory only when the task needs more detail.

Memory can become stale. Agents are instructed to treat memories as guidance only and trust the current environment. By default, memory reads have `live_update` enabled, so if the agent discovers stale memory, it can update the configured `MEMORY.md` in the same run. Disable live updates when the agent should read memory but not modify it during the run, for example if the run is latency sensitive.

## Generate memory

After a run finishes, the sandbox runtime appends that run segment to a conversation file. Accumulated conversation files are processed when the sandbox session closes.

Memory generation has two phases:

1. Phase 1: conversation extraction. A memory-generating model processes one accumulated conversation file and generates a conversation summary. System, developer, and reasoning content are omitted. If the conversation is too long, it is truncated to fit within the context window, with the beginning and end preserved. It also generates a raw memory extract: compact notes from the conversation that Phase 2 can consolidate.
2. Phase 2: layout consolidation. A consolidation agent reads raw memories for one memory layout, opens conversation summaries when more evidence is needed, and extracts patterns into `MEMORY.md` and `memory_summary.md`.

The default workspace layout is:

```text
workspace/
├── sessions/
│   └── <rollout-id>.jsonl
└── memories/
    ├── memory_summary.md
    ├── MEMORY.md
    ├── raw_memories.md (intermediate)
    ├── phase_two_selection.json (intermediate)
    ├── raw_memories/ (intermediate)
    │   └── <rollout-id>.md
    ├── rollout_summaries/
    │   └── <rollout-id>_<slug>.md
    └── skills/
```

You can configure memory generation with `MemoryGenerateConfig`:

```python
from agents.sandbox import MemoryGenerateConfig
from agents.sandbox.capabilities import Memory

memory = Memory(
    generate=MemoryGenerateConfig(
        max_raw_memories_for_consolidation=128,
        extra_prompt="Pay extra attention to what made the customer more satisfied or annoyed",
    ),
)
```

Use `extra_prompt` to tell the memory generator which signals matter most for your use case, such as customer and company details for a GTM agent.

If recent raw memories exceed `max_raw_memories_for_consolidation` (defaults to 256), Phase 2 keeps only memories from the newest conversations and removes older ones. Recency is based on the last time the conversation is updated. This forgetting mechanism helps memories reflect the newest environment.

## Multi-turn conversations

For multi-turn sandbox chats, use the normal SDK `Session` together with the same live sandbox session:

```python
from agents import Runner, SQLiteSession
from agents.run import RunConfig
from agents.sandbox import SandboxRunConfig

conversation_session = SQLiteSession("gtm-q2-pipeline-review")
sandbox = await client.create(manifest=agent.default_manifest)

async with sandbox:
    run_config = RunConfig(
        sandbox=SandboxRunConfig(session=sandbox),
        workflow_name="GTM memory example",
    )
    await Runner.run(
        agent,
        "Analyze data/leads.csv and identify one promising GTM segment.",
        session=conversation_session,
        run_config=run_config,
    )
    await Runner.run(
        agent,
        "Using that analysis, write a short outreach hypothesis.",
        session=conversation_session,
        run_config=run_config,
    )
```

Both runs append to one memory conversation file because they pass the same SDK conversation session (`session=conversation_session`) and therefore share the same `session.session_id`. This is different from the sandbox (`sandbox`), which identifies the live workspace and is not used as the memory conversation ID. Phase 1 sees the accumulated conversation when the sandbox session closes, so it can extract memory from the whole exchange instead of two isolated turns.

If you want multiple `Runner.run(...)` calls to become one memory conversation, pass a stable identifier across those calls. When memory associates a run with a conversation, it resolves in this order:

1. `conversation_id`, when you pass one to `Runner.run(...)`
2. `session.session_id`, when you pass an SDK `Session` such as `SQLiteSession`
3. `RunConfig.group_id`, when neither of the above is present
4. A generated per-run ID, when no stable identifier is present

## Use different layouts to isolate memory for different agents

Memory isolation is based on `MemoryLayoutConfig`, not on agent name. Agents with the same layout and the same memory conversation ID share one memory conversation and one consolidated memory. Agents with different layouts keep separate rollout files, raw memories, `MEMORY.md`, and `memory_summary.md`, even when they share the same sandbox workspace.

Use separate layouts when multiple agents share one sandbox but should not share memory:

```python
from agents import SQLiteSession
from agents.sandbox import MemoryLayoutConfig, SandboxAgent
from agents.sandbox.capabilities import Filesystem, Memory, Shell

gtm_agent = SandboxAgent(
    name="GTM reviewer",
    instructions="Analyze GTM workspace data and write concise recommendations.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/gtm",
                sessions_dir="sessions/gtm",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

engineering_agent = SandboxAgent(
    name="Engineering reviewer",
    instructions="Inspect engineering workspaces and summarize fixes and risks.",
    capabilities=[
        Memory(
            layout=MemoryLayoutConfig(
                memories_dir="memories/engineering",
                sessions_dir="sessions/engineering",
            )
        ),
        Filesystem(),
        Shell(),
    ],
)

gtm_session = SQLiteSession("gtm-q2-pipeline-review")
engineering_session = SQLiteSession("eng-invoice-test-fix")
```

This prevents GTM analysis from being consolidated into engineering bug-fix memory, and vice versa.
