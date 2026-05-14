"""Compaction capability sized via the litellm model registry.

The default :class:`agents.sandbox.capabilities.Compaction` ships an
OpenAI-only context-window registry. When a litellm-routed model
(Anthropic, Bedrock, Vertex, custom proxy aliases, etc.) is used,
``Compaction.sampling_params`` cannot pick a
:class:`DynamicCompactionPolicy` -- the OpenAI lookup misses -- and
falls back to a hard-coded :class:`StaticCompactionPolicy` regardless
of the model's actual input window.

:class:`LiteLLMCompaction` short-circuits that by composing a
:class:`DynamicCompactionPolicy` whose
:class:`CompactionModelInfo` carries a context window resolved through
``litellm.get_model_info()``. Two factories cover the common call shapes:

* :meth:`LiteLLMCompaction.for_model` -- when the caller only knows the
  model identifier. Performs the litellm lookup internally.
* :meth:`LiteLLMCompaction.for_context_window` -- when the caller has
  already resolved the cap (for example, after applying a per-org
  ceiling or test override). Useful when an external configuration
  layer wants to clamp the compaction threshold below the litellm-
  reported window.

Both factories accept an optional ``threshold`` fraction (default
``0.8``) -- triggering compaction this early leaves headroom for the
latest turn and the completion after older turns have been summarised.
"""

from __future__ import annotations

import logging

from typing_extensions import Self

from agents.sandbox.capabilities import (
    Compaction,
    CompactionModelInfo,
    DynamicCompactionPolicy,
)

from ..memory._optional_imports import raise_optional_dependency_error

try:
    import litellm
except ImportError as _e:
    raise_optional_dependency_error(
        "LiteLLMCompaction",
        dependency_name="litellm",
        extra_name="litellm",
        cause=_e,
    )


logger = logging.getLogger(__name__)


# Default threshold fraction. ``0.8`` triggers compaction with ~20%
# headroom for the latest turn plus completion after older turns have
# been summarised. Slightly more conservative than the upstream
# ``DynamicCompactionPolicy`` default of ``0.9`` so a tool response is
# less likely to be truncated mid-stream.
_DEFAULT_THRESHOLD_FRACTION = 0.8


# Conservative fallback when litellm has no entry for the model (brand
# new beta identifier, custom proxy alias, etc.). Sized to a 200k input
# window so a Claude-family deployment still benefits from compaction
# at a reasonable point; for a smaller model the operator will see the
# WARNING below and can pin the cap explicitly via
# :meth:`LiteLLMCompaction.for_context_window`.
_FALLBACK_CONTEXT_WINDOW = 200_000


def _litellm_context_window(model: str) -> int:
    """Resolve ``model``'s input context window via litellm.

    Falls back to :data:`_FALLBACK_CONTEXT_WINDOW` with a WARNING when
    litellm has not catalogued ``model``; the capability still
    functions (compaction kicks in at the fallback threshold) instead
    of failing the run.
    """

    try:
        info = litellm.get_model_info(model)
    except Exception as exc:  # noqa: BLE001 - litellm raises bare Exception.
        logger.warning(
            "litellm has no model info for %r (%s); LiteLLMCompaction "
            "falling back to context_window=%d.",
            model,
            exc,
            _FALLBACK_CONTEXT_WINDOW,
        )
        return _FALLBACK_CONTEXT_WINDOW

    # litellm's TypedDict marks both caps optional (Bedrock embedding
    # entries omit them, for example) so defend against None even on
    # the happy path.
    return int(info.get("max_input_tokens") or _FALLBACK_CONTEXT_WINDOW)


class LiteLLMCompaction(Compaction):
    """:class:`Compaction` whose default policy is sized for litellm models.

    Drop-in replacement for :class:`agents.sandbox.capabilities.Compaction`.
    Subclassing keeps the parent's sampling-params serialiser and
    :meth:`process_context` truncation behaviour intact; the only thing
    this class changes is how the :class:`DynamicCompactionPolicy`'s
    :class:`CompactionModelInfo` gets its ``context_window`` value --
    via litellm rather than the OpenAI-only registry.

    Construct via :meth:`for_model` or :meth:`for_context_window`;
    direct ``LiteLLMCompaction(policy=...)`` construction is also
    supported and behaves identically to the parent class (the
    classmethods are convenience wrappers, not the only entry point).
    """

    @classmethod
    def for_model(
        cls,
        model: str,
        *,
        threshold: float = _DEFAULT_THRESHOLD_FRACTION,
    ) -> Self:
        """Build sized to ``model``'s litellm-reported context window.

        Prefer this when the caller only carries the model identifier
        (for example, inside a tool that does not see the application
        configuration). If you need an external clamp (per-org
        ceiling, test override, etc.) to flow through, use
        :meth:`for_context_window` instead.
        """

        return cls.for_context_window(_litellm_context_window(model), threshold=threshold)

    @classmethod
    def for_context_window(
        cls,
        context_window: int,
        *,
        threshold: float = _DEFAULT_THRESHOLD_FRACTION,
    ) -> Self:
        """Build sized to an explicitly-provided ``context_window``.

        Use this when an external configuration layer has already
        resolved the input cap (for example, after applying a per-org
        ceiling that should clamp the compaction threshold below the
        litellm-reported window).
        """

        return cls(
            policy=DynamicCompactionPolicy(
                model_info=CompactionModelInfo(context_window=context_window),
                threshold=threshold,
            )
        )
