# Public results for external jobs

Declare a `FunctionTool` for the call the app will answer. Pass that same object to `jobs.register` and to the agent's `tools` list. Registration returns `None`. The example uses its SQLite journal to authorize each order and store its checkpoint. The provider creates or finds the thumbnail under the same order ID. The projector converts the private result into a public result that exposes only the byte count to the model.

```python
from agents import FunctionTool
from agents.experimental.deferred import DeferredTools
from examples.external_job.__main__ import ImageSize, Thumbnail, ThumbnailProvider, public_size
from examples.external_job.journal import Journal

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

Declare every public JSON key as a required field on a strict Pydantic result model that forbids extra fields. Use strings, booleans, integers, finite floats, nulls, ordered lists and nested models with the same contract. A required field can accept `None`. You can use input aliases to build the model. If an alias changes the emitted key, declare that public field name in the result model. The journal and the reply use the declared field order.

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

Keep the supplied order for ranked results. Choose public names and compute derived values in the projector. Before a job starts, registration rejects result declarations with defaults, renamed output keys, or custom transforms. If the app needs to control result serialization itself, use `render.match(event)` and submit the matched request's `reply(value)` directly through the session.

## Stable inputs

Declare generated request IDs as required fields, such as `request_id: str`, and supply the value in the original call. Preparation checks every registered sibling tool before sealing the session. Preparation rejects the batch if a manual event drops a field or selects a different model than strict validation of the original call selects. Set `extra="forbid"` on overlapping object branches so the event and provider both receive every supplied field. Use strict models that forbid extra fields, with ordinary JSON fields, ordered lists, and distinct serialized names. Scalar constants such as `attempts: int = 1` can be omitted. Registration rejects default factories, sets and custom validators or serializers. An app that needs those behaviors can answer the `FunctionTool` directly in a session. Change the versioned provider ID when you change the meaning of field names or aliases.
