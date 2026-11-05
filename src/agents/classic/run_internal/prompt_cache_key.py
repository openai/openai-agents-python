from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace as dataclass_replace
from hashlib import sha256
from typing import Any

from ..memory import Session
from ..model_settings import ModelSettings
from ..run_state import RunState
from .run_grouping import RunGroupingKind, resolve_run_grouping

PROMPT_CACHE_KEY_FIELD = "prompt_cache_key"


@dataclass
class PromptCacheKeyResolver:
    """Provides one generated prompt cache key for a runner invocation.

    The runner asks for a key on every model turn. This helper returns the same generated key each
    time, persists it to RunState for resume flows, and opts out when the request already forwards
    a user-supplied key through ModelSettings.
    """

    run_state: RunState[Any] | None = None
    _generated_key: str | None = None

    @classmethod
    def from_run_state(
        cls,
        *,
        run_state: RunState[Any] | None,
    ) -> PromptCacheKeyResolver:
        return cls(
            run_state=run_state,
            _generated_key=(
                run_state._generated_prompt_cache_key if run_state is not None else None
            ),
        )

    def resolve(
        self,
        model_settings: ModelSettings,
        *,
        model: object,
        conversation_id: str | None,
        session: Session | None,
        group_id: str | None,
    ) -> str | None:
        """Return the generated prompt cache key for this model call.

        Returns None when the runner should not add one.
        """
        # A prompt_cache_key in ModelSettings extras is already forwarded to the model adapter, so
        # the runner should not also generate one.
        if _model_settings_has_prompt_cache_key(model_settings):
            return None

        if not _model_supports_default_prompt_cache_key(model):
            return None

        return self._get_or_create_generated_key(
            conversation_id=conversation_id,
            session=session,
            group_id=group_id,
        )

    def _get_or_create_generated_key(
        self,
        *,
        conversation_id: str | None,
        session: Session | None,
        group_id: str | None,
    ) -> str:
        if self._generated_key is not None:
            return self._generated_key

        grouping_kind, grouping_value = resolve_run_grouping(
            conversation_id=conversation_id,
            session=session,
            group_id=group_id,
        )
        key = _prompt_cache_key_for_grouping(grouping_kind, grouping_value)

        self._generated_key = key
        if self.run_state is not None:
            self.run_state._generated_prompt_cache_key = key
        return key


def _model_settings_has_prompt_cache_key(model_settings: ModelSettings) -> bool:
    return _mapping_has_prompt_cache_key(
        model_settings.extra_args
    ) or _mapping_has_prompt_cache_key(model_settings.extra_body)


def model_settings_with_prompt_cache_key(
    model_settings: ModelSettings,
    prompt_cache_key: str | None,
) -> ModelSettings:
    """Return model settings with the generated prompt cache key added to extra_args."""
    if prompt_cache_key is None or _model_settings_has_prompt_cache_key(model_settings):
        return model_settings

    extra_args = dict(model_settings.extra_args or {})
    extra_args[PROMPT_CACHE_KEY_FIELD] = prompt_cache_key
    return dataclass_replace(model_settings, extra_args=extra_args)


def _model_supports_default_prompt_cache_key(model: object) -> bool:
    supports_default = getattr(model, "_supports_default_prompt_cache_key", None)
    return bool(supports_default()) if callable(supports_default) else False


def _mapping_has_prompt_cache_key(value: object) -> bool:
    return isinstance(value, Mapping) and PROMPT_CACHE_KEY_FIELD in value


def _hashed_key(kind: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"agents-sdk:{kind}:{digest}"


def _prompt_cache_key_for_grouping(kind: RunGroupingKind, value: str) -> str:
    if kind == "run":
        # With no conversation, session, or group id, reuse the key only inside this run. That
        # helps multi-turn agent loops without pretending unrelated Runner.run() calls are part
        # of the same cache group.
        return f"agents-sdk:run:{value}"
    return _hashed_key(kind, value)
