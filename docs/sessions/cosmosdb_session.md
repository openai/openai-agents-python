# Azure Cosmos DB sessions

`CosmosDBSession` stores Agents SDK conversation history in Azure Cosmos DB for NoSQL. Use it when multiple application instances need to share durable session history and Cosmos DB is already part of your deployment.

## Installation

Install the Cosmos DB extra:

```bash
pip install openai-agents[cosmosdb]
```

Token-based authentication examples also require Azure Identity, which is intentionally not included in the session extra:

```bash
pip install azure-identity
```

## Container requirements

Create the database and container before constructing a session. The backend does not provision Azure resources. The container must:

- use `/sessionId` as its partition key;
- index `/type`, `/incarnation`, and `/seq`;
- support both ascending and descending sequence queries; and
- enable container TTL when `ttl_seconds` is used.

The following indexing policy supports the session queries. Setting `defaultTtl` to `-1` enables per-item TTL without expiring items that do not have a `ttl` property.

```python
INDEXING_POLICY = {
    "automatic": True,
    "indexingMode": "consistent",
    "includedPaths": [{"path": "/*"}],
    "excludedPaths": [{"path": '/"_etag"/?'}],
    "compositeIndexes": [
        [
            {"path": "/type", "order": "ascending"},
            {"path": "/incarnation", "order": "ascending"},
            {"path": "/seq", "order": "ascending"},
        ],
        [
            {"path": "/type", "order": "ascending"},
            {"path": "/incarnation", "order": "ascending"},
            {"path": "/seq", "order": "descending"},
        ],
    ],
}
```

For example, you can provision the resources once with the Azure Cosmos DB async SDK:

```python
from azure.cosmos import PartitionKey
from azure.cosmos.aio import CosmosClient

client = CosmosClient.from_connection_string(
    connection_string,
    consistency_level="Session",
)
async with client:
    database = await client.create_database_if_not_exists("agents")
    await database.create_container_if_not_exists(
        id="agent_sessions",
        partition_key=PartitionKey(path="/sessionId"),
        indexing_policy=INDEXING_POLICY,
        default_ttl=-1,
    )
```

## Connection string quick start

`from_connection_string()` creates a Session-consistent client with non-idempotent SDK write retries disabled, validates the container by default, and transfers ownership of that client to the session.

```python
from agents import Agent, Runner
from agents.extensions.memory import CosmosDBSession

agent = Agent(name="Assistant", instructions="Reply very concisely.")
session = await CosmosDBSession.from_connection_string(
    "conversation-123",
    connection_string,
    database="agents",
    container="agent_sessions",
)

try:
    result = await Runner.run(agent, "What city is the Golden Gate Bridge in?", session=session)
    print(result.final_output)

    result = await Runner.run(agent, "What state is it in?", session=session)
    print(result.final_output)
finally:
    await session.close()
```

See [`examples/memory/cosmosdb_session_example.py`](https://github.com/openai/openai-agents-python/tree/main/examples/memory/cosmosdb_session_example.py) for a complete environment-driven example.

## Microsoft Entra ID authentication

Use `from_token_credential()` with an async credential from `azure-identity`. The session owns the Cosmos client it creates, while your application continues to own the credential.

```python
from azure.identity.aio import DefaultAzureCredential

from agents.extensions.memory import CosmosDBSession

credential = DefaultAzureCredential()
try:
    session = await CosmosDBSession.from_token_credential(
        "conversation-123",
        "https://my-account.documents.azure.com:443/",
        credential,
        database="agents",
        container="agent_sessions",
    )
    try:
        # Pass session to Runner.run(...).
        ...
    finally:
        await session.close()
finally:
    await credential.close()
```

`from_credential()` also accepts an account key or credential dictionary when an endpoint is supplied directly.

## Using an existing client

Applications that already manage a `CosmosClient` can inject it directly. The client must use Session consistency so incarnation fencing and read-after-write behavior remain correct.

```python
from azure.cosmos.aio import CosmosClient

from agents.extensions.memory import CosmosDBSession

client = CosmosClient(
    endpoint,
    credential,
    consistency_level="Session",
    retry_write=False,
)
async with client:
    session = CosmosDBSession(
        "conversation-123",
        client=client,
        database="agents",
        container="agent_sessions",
    )
    try:
        # Pass session to Runner.run(...).
        ...
    finally:
        await session.close()
```

Calling `session.close()` always makes that session instance unusable. It closes clients created by a factory, but never closes an injected client. Injected clients must set `retry_write=False` (the Azure SDK default); automatic non-idempotent write retries make destructive session operations ambiguous, so the constructor rejects clients configured to enable them.

## Validation and readiness

The factories call `validate_container()` by default and raise `CosmosSessionConfigurationError` when the database, partition key, indexes, query shapes, or TTL configuration do not satisfy the backend contract. Set `validate_on_create=False` only when deployment-time validation already enforces the same requirements.

For an injected client, validate explicitly before serving traffic:

```python
validation = await session.validate_container()
validation.raise_for_invalid()

if not await session.ping():
    raise RuntimeError("Cosmos DB is unavailable")
```

Validation does not write application data. `ContainerValidationResult.issue_codes` contains sanitized machine-readable findings.

## TTL

Pass a positive `ttl_seconds` value to apply per-item TTL to session metadata and messages:

```python
session = await CosmosDBSession.from_connection_string(
    "conversation-123",
    connection_string,
    ttl_seconds=86_400,
)
```

The container must have TTL enabled, such as `defaultTtl=-1`. Expiration is enforced by Cosmos DB and is not immediate.

## Atomicity, retries, and limits

Each non-empty `add_items()` call is one partition-scoped Cosmos transactional batch. The complete logical append becomes visible atomically. Cosmos limits transactional batches to 100 operations, so the backend rejects more than 100 items before serialization or any database mutation.

The backend reserves one deterministic sequence range and builds message documents once. It retries only transient transport and service failures. An ambiguous batch commit is replayed with the same document IDs; a `409 Conflict` is accepted only when point reads show that every application-owned field in every reserved document matches exactly.

Sequence reservation itself is not retried after an ambiguous failure because a second increment could reserve a different range. A failed append can therefore leave a harmless sequence gap without exposing a partial batch.

!!! warning

    Separate `add_items()` calls are not idempotent. Calling the method twice with the same items appends them twice. Callers that need cross-call deduplication must enforce it outside this backend; the SDK session does not expose an operation-ID API.

## Clear, delete, and corrupt records

`clear_session()` first rotates the session incarnation. That logical fence hides every pre-clear message even when best-effort physical cleanup is delayed or fails. Writes that reserved a sequence before the clear remain in the old, invisible incarnation.

`pop_item()` uses an ETag-protected internal claim before removing the current tail. An ambiguous claim response is reconciled by point read, and a claimed item is immediately hidden even when its best-effort physical cleanup is delayed.

`delete_session()` physically removes the session partition, including its metadata fence. Call it only while the session is quiescent; use `clear_session()` when other workers may still hold references to the same session ID.

Malformed message records are skipped by `get_items()` and `pop_item()` so they do not hide unrelated valid history. They are not returned or deleted by those operations. Invalid schema-v2 metadata fails closed because metadata defines the ordering and clear boundary.

Non-metadata documents with a `type` other than `"message"` are ignored by history reads.

## API reference

- [`CosmosDBSession`][agents.extensions.memory.cosmosdb_session.CosmosDBSession]
- [`ContainerValidationResult`][agents.extensions.memory.cosmosdb_session.ContainerValidationResult]
- [`CosmosSessionError`][agents.extensions.memory.cosmosdb_session.CosmosSessionError]
