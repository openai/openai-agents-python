try:
    import numpy as np
    import numpy.typing as npt
    import websockets
except ImportError as _e:
    raise ImportError(
        "`numpy` + `websockets` are required to use voice. From the matching source checkout, run "
        "`python -m pip install '.[classic-voice]'`. See CLASSIC.md for source setup."
    ) from _e

__all__ = ["np", "npt", "websockets"]
