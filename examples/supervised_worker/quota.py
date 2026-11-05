"""Account evidence for the supervised worker, supplied by the application."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from time import time
from typing import Generic, Literal, TypedDict, TypeVar, cast, get_args

from typing_extensions import Required

ReachedType = Literal[
    "rate_limit_reached",
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
]


class Credits(TypedDict, total=False):
    hasCredits: bool
    unlimited: bool
    balance: str | None


class Window(TypedDict, total=False):
    usedPercent: Required[int]
    windowDurationMins: int | None
    resetsAt: int | None


class RateLimitSnapshot(TypedDict, total=False):
    limitId: str | None
    primary: Window | None
    secondary: Window | None
    credits: Credits | None
    spendControlReached: bool | None
    rateLimitReachedType: ReachedType | None


class AccountRead(TypedDict, total=False):
    accountId: str | None
    ordinaryUsageAllowed: bool | None
    rateLimits: RateLimitSnapshot
    rateLimitsByLimitId: dict[str, object] | None


class AccountUpdate(TypedDict):
    rateLimits: RateLimitSnapshot


@dataclass(frozen=True)
class AccountBinding:
    account_id: str
    generation: int


@dataclass(frozen=True)
class Observation:
    """The binding captured by the feed, with local epoch-second observation times."""

    binding: AccountBinding
    observed_at: float
    expires_at: float


ValueT = TypeVar("ValueT")


@dataclass(frozen=True)
class Observed(Generic[ValueT]):
    value: ValueT
    observation: Observation
    source: Literal["read", "update"]

    @property
    def is_current(self) -> bool:
        """Whether this value is within the application's local age limit."""
        return self.observation.observed_at <= time() < self.observation.expires_at


@dataclass(frozen=True)
class AccountView:
    """A copy of the attached account's raw read and latest fields in its default bucket."""

    binding: AccountBinding
    read: Observed[AccountRead] | None
    rate_limits: Mapping[str, Observed[object]]


@dataclass(frozen=True)
class AccountQuotaAttention:
    binding: AccountBinding
    field: Literal["ordinaryUsageAllowed", "spendControlReached", "rateLimitReachedType"]
    value: bool | ReachedType
    turn_may_be_active: Literal[True] = True


class QuotaWatch:
    """One active account on the worker's event loop. The application owns the feed."""

    def __init__(self) -> None:
        self._binding: AccountBinding | None = None
        self._changed: asyncio.Future[None] | None = None
        self._latest = time()
        self._read: Observed[AccountRead] | None = None
        self._rate_limits: dict[str, Observed[object]] = {}

    def attach(self, account_id: str) -> AccountBinding:
        """Fence the previous feed, including when reattaching to the same account."""
        if not account_id.strip():
            raise ValueError("account_id must contain a non-whitespace character")
        generation = self._binding.generation + 1 if self._binding is not None else 1
        self._binding = AccountBinding(account_id, generation)
        self._latest = time()
        self._read = None
        self._rate_limits.clear()
        self._notify()
        return self._binding

    def account_view(self) -> AccountView | None:
        """Read the raw evidence and local freshness for the current attachment."""
        if self._binding is None:
            return None
        return AccountView(self._binding, deepcopy(self._read), deepcopy(self._rate_limits))

    def _accept(self, observation: Observation) -> bool:
        return (
            observation.binding is self._binding
            and self._latest < observation.observed_at <= time() < observation.expires_at
            and isfinite(observation.expires_at)
        )

    def observe_read(self, observation: Observation, raw: AccountRead) -> None:
        """Replace account facts after a fresh read that names the attached account."""
        if (
            not self._accept(observation)
            or raw.get("accountId") != observation.binding.account_id
            or raw.get("rateLimits") is None
        ):
            return

        read = deepcopy(raw)
        self._read = Observed(read, observation, "read")
        self._rate_limits = {
            key: Observed(value, observation, "read") for key, value in read["rateLimits"].items()
        }
        self._latest = observation.observed_at
        self._notify()

    def observe_update(self, observation: Observation, raw: AccountUpdate) -> None:
        """Merge available fields after a matching read. Retained facts keep their expiry."""
        if self._read is None or not self._accept(observation):
            return
        snapshot = raw["rateLimits"]
        limit_id = snapshot.get("limitId")
        default_id = self._read.value["rateLimits"].get("limitId") or "codex"
        if limit_id is not None and limit_id != default_id:
            return

        self._rate_limits.update(
            {
                key: Observed(deepcopy(value), observation, "update")
                for key, value in snapshot.items()
                if value is not None
            }
        )
        self._latest = observation.observed_at
        self._notify()

    def _current_value(self, field: str) -> object:
        fact = self._rate_limits.get(field)
        return fact.value if fact is not None and fact.is_current else None

    def _block(self) -> AccountQuotaAttention | None:
        read = self._read
        if read is None:
            return None
        binding = read.observation.binding
        reached = self._current_value("rateLimitReachedType")
        if reached in get_args(ReachedType):
            return AccountQuotaAttention(
                binding, "rateLimitReachedType", cast(ReachedType, reached)
            )
        if self._current_value("spendControlReached") is True:
            return AccountQuotaAttention(binding, "spendControlReached", True)
        credits = self._current_value("credits")
        if (
            read.is_current
            and read.value.get("ordinaryUsageAllowed") is False
            and isinstance(credits, dict)
            and credits.get("hasCredits") is False
            and credits.get("unlimited") is False
        ):
            return AccountQuotaAttention(binding, "ordinaryUsageAllowed", False)
        return None

    def _change(self) -> asyncio.Future[None]:
        if self._changed is None:
            self._changed = asyncio.get_running_loop().create_future()
        return self._changed

    def _notify(self) -> None:
        if self._changed is not None:
            self._changed.set_result(None)
            self._changed = None
