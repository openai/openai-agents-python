from __future__ import annotations

import pytest

from agents.exceptions import UserError
from agents.util._custom_data import normalize_custom_data


def test_normalize_custom_data_wraps_deepcopy_type_error() -> None:
    values = (value for value in [1, 2, 3])

    with pytest.raises(
        UserError,
        match="custom_data_extractor must return JSON-compatible data",
    ):
        normalize_custom_data({"values": values})
