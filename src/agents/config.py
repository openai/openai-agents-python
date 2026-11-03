"""Native types needed by recorded event and checkpoint shapes."""

from typing import TYPE_CHECKING, Literal, TypeAlias

from typing_extensions import TypeAliasType

if TYPE_CHECKING:
    JsonValue: TypeAlias = (
        "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
    )
else:
    JsonValue = TypeAliasType(
        "JsonValue", bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
    )

ReasoningEffort = TypeAliasType("ReasoningEffort", str)

ReasoningSummary = TypeAliasType("ReasoningSummary", Literal["auto", "concise", "detailed", "none"])

Verbosity = TypeAliasType("Verbosity", Literal["low", "medium", "high"])

Personality = TypeAliasType("Personality", Literal["none", "friendly", "pragmatic"])

FileSystemAccessMode = TypeAliasType("FileSystemAccessMode", Literal["read", "write", "deny"])
