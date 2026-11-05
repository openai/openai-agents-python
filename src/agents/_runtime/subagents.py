"""Native subagent configuration and query results."""

from __future__ import annotations

from dataclasses import dataclass as dataclass, field as field
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
    Annotated as Annotated,
    Any as Any,
    Literal as Literal,
)

from pydantic import (
    AfterValidator as AfterValidator,
    ConfigDict as ConfigDict,
    Field as Field,
    StringConstraints as StringConstraints,
    with_config as with_config,
)
from typing_extensions import (
    Required as Required,
    TypeAliasType as TypeAliasType,
    TypedDict as TypedDict,
)

from ..config import (
    Personality as Personality,
    ReasoningEffort as ReasoningEffort,
    ReasoningSummary as ReasoningSummary,
    Verbosity as Verbosity,
)
from ._public import preserve_module_identity as _preserve_module_identity
from .turn import Turn as Turn

if TYPE_CHECKING:
    from .input import UserInput
    from .session import Session


@dataclass(frozen=True, slots=True, init=False)
class Subagent:
    """One captured native descendant with its exact observed turn.

    Reading descendants never starts or resumes them. start() sends input only
    to this idle, live incarnation; a closed or reloaded child rejects it.
    status describes the graph edge. Inspect is_live before selecting children
    for interaction; restored graph rows can be active without a loaded runtime.
    """

    id: str
    session_id: str
    name: str | None
    parent_agent_id: str
    status: Literal["active", "closed"]
    opened_at: int
    closed_at: int | None
    turn: Turn[str] | None
    _session: Session[Any, Any] = field(repr=False, compare=False)
    _instance_id: int | None = field(repr=False, compare=False)
    _incarnation: str | None = field(repr=False, compare=False)

    def __new__(cls, *args: Any, **kwargs: Any) -> Subagent:
        raise TypeError("Subagents are issued by Session.subagents()")

    def __reduce__(self) -> Any:
        raise TypeError("Subagent belongs to a live Session and cannot be serialized")

    @property
    def is_live(self) -> bool:
        """Whether this snapshot captured a loaded, active native instance.

        This value does not update after the snapshot. A later close or reload
        can expire the instance, so start() still validates it. Get a new
        snapshot from Session.subagents() after a child is reloaded.
        """
        return (
            self.status == "active"
            and self._instance_id is not None
            and self._incarnation is not None
        )

    async def start(self, input: UserInput) -> Turn[str]:
        """Start this child only when idle and return its exact Turn.

        Use Turn.steer on a live turn. If input arrives after its final sample,
        a successful turn can start an automatic successor. Follow that turn
        through Session.events() or a fresh Session.subagents() snapshot.
        """
        return await self._session._start_child(self, input)

    async def try_cancel(self, turn: Turn[str]) -> bool:
        """Stop this exact turn and join its cooperative callbacks and cells.

        True confirms native interruption and joined SDK-owned cleanup. False
        means the native interruption was not confirmed. Cancelling this waiter
        still waits for cleanup; noncooperative callbacks can delay it.
        """
        return await self._session._try_cancel_child(self, turn)


def _subagent(
    session: Session[Any, Any],
    data: dict[str, Any],
    turn: Turn[str] | None,
    instance_id: int | None,
    incarnation: str | None,
) -> Subagent:
    result = object.__new__(Subagent)
    for name, value in {
        "id": data["id"],
        "session_id": data["session_id"],
        "name": data["name"],
        "parent_agent_id": data["parent_agent_id"],
        "status": data["status"],
        "opened_at": data["opened_at"],
        "closed_at": data["closed_at"],
        "turn": turn,
        "_session": session,
        "_instance_id": instance_id,
        "_incarnation": incarnation,
    }.items():
        object.__setattr__(result, name, value)
    return result


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("Role instructions and descriptions cannot be blank")
    return value


def _unique_names(names: list[str]) -> list[str]:
    if len(names) != len(set(names)):
        raise ValueError("Role nicknames must be unique")
    return names


_Nonblank = TypeAliasType("_Nonblank", Annotated[str, AfterValidator(_nonblank)])
_Nickname = TypeAliasType(
    "_Nickname",
    Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[a-zA-Z0-9 _-]+$"),
    ],
)


@with_config(ConfigDict(extra="forbid", strict=True))
class SubagentConfig(TypedDict, total=False, closed=True):
    """Stored native role settings; omitted fields inherit.

    Developer instructions replace inherited native developer instructions.
    The role name is the key in the stored role mapping. Declare new roles with
    ``NativeRole`` in ``NativeAgents.additional_roles``.
    """

    description: Required[_Nonblank]
    developer_instructions: _Nonblank
    model: str
    model_reasoning_effort: Annotated[ReasoningEffort, Field(min_length=1)]
    model_reasoning_summary: ReasoningSummary
    model_verbosity: Verbosity
    personality: Personality
    nickname_candidates: Annotated[
        list[_Nickname], Field(min_length=1), AfterValidator(_unique_names)
    ]


_preserve_module_identity(globals(), "agents.subagents")
