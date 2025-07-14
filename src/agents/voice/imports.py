try:
    import numpy as np
    import numpy.typing as npt
    import websockets
except ImportError:
    # fixed: removed unused exception variable _e
    raise ImportError(
        "`numpy` + `websockets` are required to use voice. You can install them via the optional "
        "dependency group: `pip install 'openai-agents[voice]'`."
    )

__all__ = ["np", "npt", "websockets"]
