# Dapr State Store Sessions

The `DaprSession` class provides production-grade, distributed session memory using Dapr state stores. This enables your agents to scale horizontally across multiple application instances while maintaining conversation context in various backends like Redis, PostgreSQL, MongoDB, Cassandra, and many others as shown in the [Dapr state store documentation](https://docs.dapr.io/reference/components-reference/supported-state-stores/).

## Overview

[Dapr](https://dapr.io) is a portable, event-driven runtime that simplifies building resilient applications. The `DaprSession` class integrates the OpenAI Agents SDK with Dapr's state management, giving you:

- **Backend flexibility**: Use any of 30+ state stores (Redis, PostgreSQL, MongoDB, Cosmos DB, etc.) without code changes
- **Production features**: TTL, consistency levels, and automatic retries via Dapr
- **Separation of concerns**: Developers focus on agents while platform teams manage infrastructure and policies
- **Cloud-native deployment**: Seamless Kubernetes integration with sidecar pattern

## Installation

Install the agents SDK with Dapr support:

```bash
pip install openai-agents[dapr]
```

This installs the required dependencies:
- `dapr>=1.14.0` - Official Dapr Python SDK
- `grpcio>=1.60.0` - gRPC communication with Dapr sidecar

## Quick start

### Running locally

First, start the Dapr sidecar with a state store. The simplest way is using Redis:

```bash
# Start Redis (if not already running)
docker run -d -p 6379:6379 redis:7-alpine

# Start Dapr sidecar with Redis state store
dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components
```

Create a component configuration file at `./components/statestore.yaml`:

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: localhost:6379
  - name: redisPassword
    value: ""
```

Now use DaprSession in your code:

```python
from agents import Agent, Runner
from agents.extensions.memory import DaprSession

# Create agent
agent = Agent(
    name="Assistant",
    instructions="Reply very concisely.",
)

# Connect to Dapr sidecar (using default gRPC port 50001)
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",  # Default Dapr gRPC port
)

# First turn
result = await Runner.run(
    agent,
    "What city is the Golden Gate Bridge in?",
    session=session
)
print(result.final_output)  # "San Francisco"

# Second turn - agent remembers context
result = await Runner.run(
    agent,
    "What state is it in?",
    session=session
)
print(result.final_output)  # "California"

# Clean up
await session.close()
```

## Usage patterns

### Dapr ports

The DaprSession connects to the Dapr sidecar via gRPC. Dapr uses these default ports:

- **gRPC port: 50001** (used by DaprSession) - For programmatic API access
- **HTTP port: 3500** - For REST API and health checks
- **Metrics port: 9090** - For Prometheus metrics

When starting the Dapr sidecar, you can specify custom ports if needed:

```bash
# Using default ports (recommended)
dapr run --app-id myapp --components-path ./components

# Using custom ports
dapr run --app-id myapp \
  --dapr-grpc-port 50002 \
  --dapr-http-port 3501 \
  --components-path ./components
```

If using custom ports, specify them in your session connection:

```python
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50002",  # Custom gRPC port
)
```

**Note**: In Kubernetes, the Dapr sidecar always uses the default ports on localhost.

### Connection methods

There are two ways to create a DaprSession:

**1. Using `from_address()` (recommended)**

The session creates and manages its own Dapr client:

```python
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
)
```

**2. Using an existing Dapr client**

Useful when you want to manage the Dapr client lifecycle yourself:

```python
from dapr.clients import DaprClient

# Create and manage your own client
dapr_client = DaprClient(address="localhost:50001")

session = DaprSession(
    session_id="user-123",
    state_store_name="statestore",
    dapr_client=dapr_client,
)

# You're responsible for closing the client
await session.close()  # Won't close the external client
dapr_client.close()
```

### Time-to-live (TTL)

Configure automatic session expiration:

```python
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
    ttl=3600,  # Expire after 1 hour
)
```

TTL support varies by state store. Check your store's [documentation](https://docs.dapr.io/reference/components-reference/supported-state-stores/) for TTL capabilities.

### Consistency levels

Control read/write consistency for state operations. Use the provided constants to avoid typos:

```python
from agents.extensions.memory import (
    DaprSession,
    CONSISTENCY_EVENTUAL,
    CONSISTENCY_STRONG,
)

# Eventual consistency (default, better performance)
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
    consistency=CONSISTENCY_EVENTUAL,  # or "eventual"
)

# Strong consistency (guarantees read-after-write consistency)
session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
    consistency=CONSISTENCY_STRONG,  # or "strong"
)
```

**Important**: Consistency levels apply to both read and write operations. When using `CONSISTENCY_STRONG`, the session ensures that reads always reflect the most recent writes, preventing stale data after updates.

Support varies by state store. See [Dapr consistency documentation](https://docs.dapr.io/developing-applications/building-blocks/state-management/state-management-overview/#consistency).

### Session isolation

Each session ID maintains its own isolated conversation history:

```python
# Different users have separate conversations
user_1_session = DaprSession.from_address(
    session_id="user-1",
    state_store_name="statestore",
    dapr_address="localhost:50001",
)

user_2_session = DaprSession.from_address(
    session_id="user-2",
    state_store_name="statestore",
    dapr_address="localhost:50001",
)

# Each session maintains independent conversation history
await Runner.run(agent, "I like cats", session=user_1_session)
await Runner.run(agent, "I like dogs", session=user_2_session)
```

## State store configuration

DaprSession works with any Dapr-supported state store. Create a component YAML file in your components directory (e.g., `./components/statestore.yaml`). Here are minimal examples:

### Redis

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.redis
  version: v1
  metadata:
  - name: redisHost
    value: localhost:6379
```

[Full Redis configuration options →](https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-redis/)

### PostgreSQL (v2 recommended)

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.postgresql
  version: v2
  metadata:
  - name: connectionString
    value: "host=localhost user=postgres password=postgres dbname=dapr port=5432"
```

[Full PostgreSQL configuration options →](https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-postgresql-v2/)

### MongoDB

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.mongodb
  version: v1
  metadata:
  - name: host
    value: "localhost:27017"
```

[Full MongoDB configuration options →](https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-mongodb/)

### Azure Cosmos DB

```yaml
apiVersion: dapr.io/v1alpha1
kind: Component
metadata:
  name: statestore
spec:
  type: state.azure.cosmosdb
  version: v1
  metadata:
  - name: url
    value: "https://<your-account>.documents.azure.com:443/"
  - name: masterKey
    value: "<your-master-key>"
  - name: database
    value: "dapr"
```

[Full Azure Cosmos DB configuration options →](https://docs.dapr.io/reference/components-reference/supported-state-stores/setup-azure-cosmosdb/)

**Important**: Always use secret references for passwords and keys in production. See [Dapr secrets documentation](https://docs.dapr.io/operations/components/component-secrets/).

[View all 30+ supported state stores →](https://docs.dapr.io/reference/components-reference/supported-state-stores/)

## Production deployment

### Kubernetes deployment

Deploy your application with Dapr sidecar in Kubernetes:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agents-app
spec:
  replicas: 3
  selector:
    matchLabels:
      app: agents-app
  template:
    metadata:
      labels:
        app: agents-app
      annotations:
        dapr.io/enabled: "true"
        dapr.io/app-id: "agents-app"
        dapr.io/app-port: "8080"
        dapr.io/config: "dapr-config"
    spec:
      containers:
      - name: agents-app
        image: your-registry/agents-app:latest
        ports:
        - containerPort: 8080
        env:
        - name: DAPR_GRPC_PORT
          value: "50001"
```

Your Python application code:

```python
import os
from agents.extensions.memory import DaprSession

# Dapr sidecar is available at localhost
session = DaprSession.from_address(
    session_id=user_id,
    state_store_name="statestore",
    dapr_address="localhost:50001",
)
```

### Multi-tenancy

Use session ID prefixes to isolate tenants:

```python
def get_session(tenant_id: str, user_id: str) -> DaprSession:
    session_id = f"{tenant_id}:{user_id}"
    return DaprSession.from_address(
        session_id=session_id,
        state_store_name="statestore",
        dapr_address="localhost:50001",
    )

# Tenant A
tenant_a_session = get_session("tenant-a", "user-123")

# Tenant B
tenant_b_session = get_session("tenant-b", "user-123")
```

### Error handling

```python
from agents.extensions.memory import DaprSession

session = DaprSession.from_address(
    session_id="user-123",
    state_store_name="statestore",
    dapr_address="localhost:50001",
)

try:
    # Test connectivity
    if not await session.ping():
        print("Dapr sidecar is not available!")
        return

    # Use the session
    result = await Runner.run(agent, "Hello", session=session)

except Exception as e:
    print(f"Error using Dapr session: {e}")

finally:
    await session.close()
```

## Comparison with other session types

| Feature | DaprSession | RedisSession | SQLAlchemySession | SQLiteSession |
|---------|-------------|--------------|-------------------|---------------|
| Backend flexibility | 30+ stores | Redis only | Any SQL DB | SQLite only |
| Horizontal scaling | ✓ | ✓ | ✓ | ✗ |
| Cloud-native | ✓ | Partial | Partial | ✗ |
| TTL support | Store-dependent | ✓ | Store-dependent | ✗ |
| Consistency control | ✓ | ✗ | Store-dependent | ✓ |
| Setup complexity | Medium | Low | Medium | Very Low |
| Performance | Very High | Very High | High | High |

## Troubleshooting

### Dapr sidecar not reachable

Ensure the Dapr sidecar is running and accessible:

```python
session = DaprSession.from_address(
    session_id="test",
    state_store_name="statestore",
    dapr_address="localhost:50001",
)

if await session.ping():
    print("Connected to Dapr!")
else:
    print("Cannot reach Dapr sidecar")
```

Common issues:
- Check that `dapr run` is active (use `dapr list` to see running sidecars)
- Verify the gRPC port matches (default: 50001)
- Ensure no firewall blocking the port
- Check that the Dapr sidecar finished initializing (check logs with `dapr logs --app-id myapp`)

### State store not configured

Error: `state store statestore is not found`

Solution: Ensure your component configuration is in the components path specified when starting Dapr:

```bash
dapr run --app-id myapp --dapr-grpc-port 50001 --components-path ./components
```

### TTL not working

Verify your state store [supports TTL](https://docs.dapr.io/reference/components-reference/supported-state-stores/) and is properly configured.

## API reference

For detailed API documentation, see:

- [`DaprSession`][agents.extensions.memory.dapr_session.DaprSession] - Full API reference
- [`Session`][agents.memory.session.Session] - Protocol interface

## Additional resources

- [Dapr State Management](https://docs.dapr.io/developing-applications/building-blocks/state-management/)
- [Dapr State Store Components](https://docs.dapr.io/reference/components-reference/supported-state-stores/)
- [Dapr Python SDK](https://github.com/dapr/python-sdk)

