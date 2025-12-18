from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel, ValidationError

from agents.util._pydantic import coerce_model_with_literal_fallback


def test_coerce_model_with_literal_fallback_accepts_literal_miss():
    class LiteralToyModel(BaseModel):
        kind: str
        mode: Literal["a", "b"]

    obj = coerce_model_with_literal_fallback(
        LiteralToyModel,
        {"kind": "x", "mode": "c"},
        literal_error_locs=[("mode",)],
    )
    assert isinstance(obj, LiteralToyModel)
    assert str(obj.mode) == "c"


def test_coerce_model_with_literal_fallback_propagates_other_errors():
    class OtherModel(BaseModel):
        field: int

    with pytest.raises(ValidationError):
        coerce_model_with_literal_fallback(OtherModel, {"field": "oops"}, literal_error_locs=[])
