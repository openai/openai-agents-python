"""
Internal helpers shared by the agent run pipeline. Public-facing APIs (e.g., RunConfig,
RunOptions) belong at the top-level; only execution-time utilities that are not part of the
surface area should live under run_internal.
"""

from __future__ import annotations
