# Run and resume an external thumbnail job

Start a thumbnail request, render it in a separate worker, and resume the original tool call. The application keeps the image and provider receipt private and returns only the byte count to the model.

## Set up the job

Install the [matching SDK preview wheel](../../../docs/sdk/installation.md), activate its Python environment, and open the repository root containing `agents.experimental.deferred` and these example files. Use a local POSIX filesystem with a Unix SQLite VFS that creates private files using the selected state directory's permissions.

Set `THUMBNAIL_STATE_DIR` to an absolute private state path outside every model-visible mount. Its parent directory must already exist and be trusted by the host. Keep the database, SQLite sidecars, locks, and private receipts outside those mounts for the job's entire lifetime. The example trusts processes with the same operating-system user ID.

The `run` command needs a local Responses endpoint. The example sends a fixed `local-only` credential to that endpoint and does not use `OPENAI_API_KEY`. The worker needs no model connection.

## Start the synthetic model endpoint

In one terminal, keep the SDK environment activated and run from the repository root:

```sh
PYTHONPATH=. python -m examples.codex.external_job.model
```

The command prints `MODEL_PORT=` followed by its selected port when the loopback server is ready. Keep a direct connection or proxy bypass for `127.0.0.1`. The server uses `ScriptedResponses` to request one blue thumbnail and finish after the resumed call returns its byte count. No API key or additional package is needed. It does not evaluate live model behavior.

In a second terminal, activate the same environment and open the same repository root. Set `MODEL_PORT` to the number printed by the first terminal, then set `THUMBNAIL_STATE_DIR` to the private absolute path described above. Use a new, empty state directory for this demonstration; an already completed order would not consume the new model script.

You can also supply your own local Responses endpoint. It must request `render_thumbnail` and continue after receiving its result.

## Render and resume an order

Run these commands in order from the repository root:

```sh
PYTHONPATH=. python -m examples.codex.external_job run sample-01 --model-port "$MODEL_PORT" --directory "$THUMBNAIL_STATE_DIR"
PYTHONPATH=. python -m examples.codex.external_job worker sample-01 --directory "$THUMBNAIL_STATE_DIR"
PYTHONPATH=. python -m examples.codex.external_job run sample-01 --model-port "$MODEL_PORT" --directory "$THUMBNAIL_STATE_DIR"
```

The first command prints `pending`. The worker stores a 32 by 32 image in the private application database and prints `Rendered.`. The final command restores the pending call, gives the model the image's byte count, and prints `completed`.

After the final command, the first terminal prints `Verified thumbnail result; model endpoint closed.` and exits. The endpoint checks the actual returned byte count and serves only this one order. Start a fresh endpoint for another demonstration. To stop before completion, press Ctrl+C in the endpoint terminal; this closes the local server without deleting the order's private state.

Keep the state directory if you want to inspect the saved order. After all three commands and the endpoint have exited, you can delete that application-chosen directory to remove the demonstration's database, image, checkpoints, and private receipts. Deletion prevents later resume.

Reuse the order ID when retrying the same work. Choose a new ID to request another image. The SQLite journal authorizes each order and stores its checkpoint. The provider creates or finds the thumbnail under the same order ID.

## Register the public result

Declare a `FunctionTool` for the call the app will answer. Pass that same object to `jobs.register` and to the agent's `tools` list. Registration returns `None`.

The projector converts the private result into a public result that exposes only the byte count to the model. The following registration excerpt uses the example's journal and provider. From the repository root, set `PYTHONPATH=.` for its `examples.codex.external_job` imports:

```python
from agents import FunctionTool
from agents.experimental.deferred import DeferredTools
from examples.codex.external_job.__main__ import (
    ImageSize,
    Thumbnail,
    ThumbnailProvider,
    public_size,
)
from examples.codex.external_job.journal import Journal

render = FunctionTool(
    name="render_thumbnail",
    description="Render one thumbnail.",
    input_type=Thumbnail,
    output_type=ImageSize,
)


def jobs_for(journal: Journal) -> DeferredTools:
    jobs = DeferredTools(journal=journal, tenant=journal.store.tenant, run_id=journal.order)
    jobs.register(
        render,
        provider_id="sqlite-thumbnail-v1",
        provider=ThumbnailProvider(journal),
        project=public_size,
        max_output_bytes=64,
        require_reconciliation=True,
    )
    return jobs
```

## Declare public results

Use a strict Pydantic result model that forbids extra fields:

- Declare every public JSON key as a required field. Required fields can accept `None`.
- Use strings, booleans, integers, finite floats, nulls, ordered lists, and nested models with the same contract.
- You can use input aliases to build the model. If an alias changes the emitted key, declare that public field name in the result model.

The journal and reply use the declared field order.

For example, an app that promises alphabetical labels can convert a private set into a sorted public list.

```python
from pydantic import BaseModel, ConfigDict


class PublicLabels(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    labels: list[str]


def public_labels(private: object) -> PublicLabels:
    if not isinstance(private, set) or not all(isinstance(label, str) for label in private):
        raise ValueError("Expected a set of labels")
    return PublicLabels(labels=sorted(private))
```

Keep the supplied order for ranked results. Choose public names and compute derived values in the projector. Before a job starts, registration rejects result declarations with defaults, renamed output keys, or custom transforms.

If the app needs to control result serialization itself, use `render.match(event)` and call `await request.respond(value)` on the matched request. In an `Agent.run()` interaction handler, return `request.reply(value)` instead.

## Declare stable inputs

Declare inputs with these constraints:

- Make generated request IDs required, such as `request_id: str`, and supply them in the original call.
- Use strict models that forbid extra fields, with ordinary JSON fields, ordered lists, and distinct serialized names.
- Set `extra="forbid"` on overlapping object branches so the event and provider both receive every supplied field.
- Scalar constants such as `attempts: int = 1` can be omitted. Registration rejects default factories, sets, and custom validators or serializers.

Preparation checks every registered sibling tool before sealing the session. It rejects the batch if a manual event drops a field or selects a different model than strict validation of the original call selects.

If the app needs unsupported input behaviors, answer the `FunctionTool` directly in a session. Change the versioned provider ID when you change the meaning of field names or aliases.
