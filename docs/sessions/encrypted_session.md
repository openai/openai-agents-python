# Encrypted sessions

`EncryptedSession` provides transparent encryption for any session implementation, securing conversation data with automatic expiration of old items.

## Features

- **Transparent encryption**: Wraps any session with Fernet encryption
- **Per-session keys**: Uses HKDF key derivation for unique encryption per session
- **Automatic expiration**: Old items are silently skipped when TTL expires
- **Drop-in replacement**: Works with any existing session implementation

## Installation

Encrypted sessions require the `encrypt` extra:

```bash
pip install 'openai-agents[encrypt]'
```

## Quick start

This example uses an in-memory `SQLiteSession` and generates a fresh encryption key for each execution. The built-in session does not require a separate database driver. For persistent storage, follow the [encryption key guidance](#encryption-key) to retain and reuse the key across process restarts.

```python
import asyncio
from cryptography.fernet import Fernet
from agents import Agent, Runner, SQLiteSession
from agents.extensions.memory import EncryptedSession

async def main():
    agent = Agent("Assistant")
    encryption_key = Fernet.generate_key().decode("ascii")

    underlying_session = SQLiteSession("user-123")
    try:
        session = EncryptedSession(
            session_id="user-123",
            underlying_session=underlying_session,
            encryption_key=encryption_key,
            ttl=600  # 10 minutes
        )

        result = await Runner.run(agent, "Hello", session=session)
        print(result.final_output)
    finally:
        underlying_session.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Configuration

### Encryption key

Use a cryptographically random, high-entropy master key, such as a key generated with [`Fernet.generate_key()`](https://cryptography.io/en/latest/fernet/#cryptography.fernet.Fernet.generate_key), or a high-entropy random secret provisioned by your application's secret-management system. Do not use a password, a memorable phrase, or a hard-coded sample value as the encryption key.

```python
from cryptography.fernet import Fernet

# Generate once when provisioning a new key, then store the value securely.
encryption_key = Fernet.generate_key().decode("ascii")
```

For persistent storage, generate the key once and save the value in a secret manager or another secure store before using the key to encrypt data. Load the same key on every process start. The following snippets assume that your deployment injects the stored key as `SESSION_ENCRYPTION_KEY`; the environment variable name is an application convention, not an SDK setting.

```python
import os
from agents.extensions.memory import EncryptedSession

encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=600
)
```

Keep the key secret, and keep it separate from the encrypted session database. To read existing data after a restart, use the same master key and the same `session_id`. Generating a replacement key at startup prevents the application from decrypting existing records. Changing `encryption_key` does not re-encrypt stored records; existing records still require the original key and remain subject to their TTL.

`EncryptedSession` continues to accept raw strings for backward compatibility. That acceptance is not a recommendation to use passwords or other low-entropy secrets. The SDK uses HKDF for per-session key derivation, not password hardening. The session ID salt separates session keys but adds no secret entropy. See [Key derivation](#key-derivation) below.

### TTL (time to live)

Set how long encrypted items remain valid. These snippets reuse `encryption_key` loaded above:

```python
# Items expire after 1 hour
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=3600  # 1 hour in seconds
)

# Items expire after 1 day
session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying_session,
    encryption_key=encryption_key,
    ttl=86400  # 24 hours in seconds
)
```

## Usage with different session types

### With SQLite sessions

```python
import os
from agents import SQLiteSession
from agents.extensions.memory import EncryptedSession

# Load the same securely stored key each time this database is opened.
encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]

# Create encrypted SQLite session
underlying = SQLiteSession("user-123", "conversations.db")

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key=encryption_key
)
```

### With SQLAlchemy sessions

Install the `encrypt` and `sqlalchemy` extras for the PostgreSQL example below. The `sqlalchemy` extra includes the `asyncpg` driver used by the `postgresql+asyncpg://` URL.

```bash
pip install 'openai-agents[encrypt,sqlalchemy]'
```

```python
import os
from agents.extensions.memory import EncryptedSession, SQLAlchemySession

# Load the same securely stored key each time this database is opened.
encryption_key = os.environ["SESSION_ENCRYPTION_KEY"]

# Create encrypted SQLAlchemy session
underlying = SQLAlchemySession.from_url(
    "user-123",
    url="postgresql+asyncpg://user:pass@localhost/db",
    create_tables=True
)

session = EncryptedSession(
    session_id="user-123",
    underlying_session=underlying,
    encryption_key=encryption_key
)
```

The application is responsible for disposing of the engine created by `SQLAlchemySession.from_url()`. After all `SQLAlchemySession` instances using that engine are no longer needed, call `await underlying.engine.dispose()` in the application's cleanup path, even if a run fails.

!!! warning "Advanced Session Features"

    When using `EncryptedSession` with advanced session implementations like `AdvancedSQLiteSession`, note that:

    - Methods like `find_turns_by_content()` won't work effectively since message content is encrypted
    - Content-based searches operate on encrypted data, limiting their effectiveness



## Key derivation

EncryptedSession uses HKDF (HMAC-based Key Derivation Function) to derive unique encryption keys per session:

- **Master key**: Your provided encryption key
- **Session salt**: The session ID
- **Info string**: `"agents.session-store.hkdf.v1"`
- **Output**: 32-byte Fernet key

With a high-entropy master key, different session IDs produce different derived keys. Reusing the same master key and session ID produces the same derived key, allowing the application to decrypt previously stored, unexpired items.

HKDF does not make a weak master key resistant to password guessing. The session ID is a non-secret salt, not additional secret key material. See the [cryptography HKDF documentation](https://cryptography.io/en/latest/hazmat/primitives/key-derivation-functions/#hkdf) for the distinction between key derivation and password hardening.

## Automatic expiration

When items exceed the TTL, they are automatically skipped during retrieval:

```python
# Items older than TTL are silently ignored
items = await session.get_items()  # Only returns non-expired items

# Expired items don't affect session behavior
result = await Runner.run(agent, "Continue conversation", session=session)
```

## API reference

- [`EncryptedSession`][agents.extensions.memory.encrypt_session.EncryptedSession] - Main class
- [`Session`][agents.memory.session.Session] - Base session protocol
