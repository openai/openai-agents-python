# Dapr State Store Sessions

The `DaprSession` class provides distributed session memory for production agents.

It enables horizontal scaling across multiple instances while maintaining conversation context. Choose from [30+ backends](https://docs.dapr.io/reference/components-reference/supported-state-stores/): Redis, PostgreSQL, MongoDB, Cosmos DB, and more.

## Overview

[Dapr](https://dapr.io) is a portable, event-driven runtime that simplifies building resilient applications. 

The `DaprSession` class integrates the OpenAI Agents SDK with Dapr's state management, giving you:

- **Backend flexibility**: Use any of 30+ state stores without code changes
- **Production features**: TTL, consistency levels, and automatic retries via Dapr
- **Separation of concerns**: Developers focus on agents while platform teams manage infrastructure
- **Cloud-native deployment**: Seamless Kubernetes integration (Dapr runs as a sidecar container alongside your app)

## When to use DaprSession

### Ideal use cases

Choose `DaprSession` when you have:

- **Horizontally scaled deployments**: Multiple agent instances behind a load balancer need to share conversation state
  - *Example*: A customer service chatbot deployed across 10+ Kubernetes pods
- **Multi-region requirements**: Your agents run in different geographic regions and need consistent state
  - *Example*: Global support system where users can be served from any region
- **Existing Dapr adoption**: Your team already uses Dapr for other microservices
  - *Example*: Your organization has standardized on Dapr for service mesh, pub/sub, and state management
- **Backend flexibility requirements**: You need to switch state stores without redeploying code
  - *Example*: Starting with Redis in dev, moving to Cosmos DB in production
- **Enterprise governance**: Platform teams need centralized control over state management policies
  - *Example*: Security requires encryption, TTL, and audit logging configured at the infrastructure level

### When to consider alternatives

**Use `SQLiteSession` instead if**:
- Your agent runs as a single instance (desktop app, CLI tool, personal assistant)
- You want zero external dependencies

**Use `Session` (in-memory) instead if**:
- You're building a quick prototype or demo
- Sessions are short-lived and losing state on restart is acceptable

### The trade-off

`DaprSession` adds operational complexity (running Dapr sidecars, managing components) in exchange for production-grade features, flexibility and governance. Choose it when that trade-off makes sense for your deployment scale and requirements.

## Installation

Install the agents SDK with Dapr support:

```bash
pip install openai-agents[dapr]
```

This installs the required dependencies:
- `dapr>=1.14.0` - Official Dapr Python SDK
- `grpcio>=1.60.0` - gRPC (a high-performance RPC framework) for communication with Dapr sidecar

## Quick start

### Prerequisites

Before starting, ensure you have:

- **Docker** installed and running ([install guide](https://docs.docker.com/get-docker/))
- **Dapr CLI** installed ([install guide](https://docs.dapr.io/getting-started/install-dapr-cli/))
- **Python 3.10+** with pip
- Basic familiarity with your chosen state store (Redis recommended for getting started)

**Note**: The Dapr CLI initializes Dapr in your local environment. Run `dapr init` after installing the CLI.

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

**What's happening here?**
1. The Dapr sidecar connects to Redis and provides a state management API
2. `DaprSession` stores conversation history in the state store (identified by `session_id`)
3. The agent can retrieve previous messages across multiple turns
4. When you scale to multiple instances, all instances share the same session state

## Usage patterns

### Dapr ports

The `DaprSession` communicates with the Dapr sidecar using network ports. Dapr exposes these default ports:

- **gRPC port: 50001** (used by `DaprSession`) - For programmatic API access from your application
- **HTTP port: 3500** - For REST API and health checks
- **Metrics port: 9090** - For Prometheus metrics (monitoring)

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

TTL (Time-To-Live) automatically expires session data after a specified duration. This is useful for limiting storage costs and ensuring stale sessions are cleaned up.

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

Consistency levels control how Dapr handles read/write operations across distributed systems:

- **Eventual consistency**: Faster performance, but reads might temporarily return stale data
- **Strong consistency**: Guarantees reads always return the latest data, with slightly higher latency

Use the provided constants to avoid typos:

```python
from agents.extensions.memory.dapr_session import (
    CONSISTENCY_EVENTUAL,
    CONSISTENCY_STRONG,
    DaprSession,
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

### State encryption

Dapr provides built-in automatic encryption at the state store level using AES-GCM (128, 192, or 256-bit keys). This enables encryption at rest without application code changes:

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
  - name: primaryEncryptionKey
    secretKeyRef:
      name: mysecret
      key: mykey
  # Optional: for key rotation
  - name: secondaryEncryptionKey
    secretKeyRef:
      name: mysecret2
      key: mykey2
```

**Key features:**
- Automatic encryption/decryption handled by Dapr
- Support for key rotation with primary/secondary keys
- Works with all Dapr state stores
- Encryption keys are fetched from secrets (never plaintext)

For detailed information on encryption configuration and key rotation strategies, see [Dapr's state encryption documentation](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-encrypt-state/).

**Note**: Dapr's state-level encryption is complementary to the SDK's `EncryptedSession` wrapper, which provides application-level encryption. Choose the approach that best fits your security requirements:
- **Dapr encryption**: Infrastructure-level, transparent to application code
- **`EncryptedSession`**: Application-level, works with any session backend (Redis, SQLite, etc.)

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

Dapr provides multiple strategies for multi-tenant architectures. You can choose the approach that best fits your needs:

#### Application-level tenant isolation

One approach is to use session ID prefixes to isolate tenants at the application level:

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

# Tenant B (isolated from Tenant A)
tenant_b_session = get_session("tenant-b", "user-123")
```

#### Dapr state sharing strategies

Dapr also supports different key prefix strategies at the state store level through the `keyPrefix` metadata option:

- **`appid` (default)**: State is scoped to each application ID
- **`namespace`**: State is scoped by Kubernetes namespace, allowing multiple apps in different namespaces to use the same state store
- **`name`**: State is scoped by state store name, allowing sharing across applications
- **`none`**: No prefixing, enabling full state sharing

For more details on these strategies, see [Dapr's state sharing documentation](https://docs.dapr.io/developing-applications/building-blocks/state-management/howto-share-state/).

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

### Connection issues: Dapr sidecar not reachable

If your application can't connect to Dapr, verify the sidecar is running and accessible:

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

### Configuration error: State store not found

**Error message**: `state store statestore is not found`

**Cause**: Dapr can't find the state store component configuration.

**Solution**: Ensure your component YAML file is in the components directory specified when starting Dapr:

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

