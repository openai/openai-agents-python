"""Voice model implementations (STT, TTS and realtime) bundled with the SDK.

This file merely re-exports the concrete classes so downstream code can do:

```python
from agents.voice.models import OpenAISTTModel, OpenAITTSModel
```

Historically this module existed; it was accidentally deleted during the
realtime-pipeline work.  Restoring it keeps the public import surface
stable for existing applications that rely on the old path.
"""

from __future__ import annotations

__all__: list[str] = [
    # STT / TTS
    "OpenAISTTModel",
    "OpenAISTTTranscriptionSession",
    "OpenAITTSModel",
    # Realtime models
    "OpenAIRealtimeLLM",
    "SDKRealtimeLLM",
]

# Speech-to-text / text-to-speech
from .openai_stt import OpenAISTTModel, OpenAISTTTranscriptionSession  # noqa: E402
from .openai_tts import OpenAITTSModel  # noqa: E402

# Realtime voice LLMs
from .sdk_realtime import SDKRealtimeLLM  # noqa: E402
