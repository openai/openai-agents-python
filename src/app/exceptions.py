"""
Thin re-export layer so the rest of our code can just do
    from .exceptions import ModelBehaviorError, UserError
without depending on the SDK’s namespace directly.
"""
from agents.exceptions import ModelBehaviorError, UserError   # type: ignore

__all__ = ["ModelBehaviorError", "UserError"]
