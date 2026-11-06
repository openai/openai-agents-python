from __future__ import annotations

from dataclasses import fields, is_dataclass
from types import UnionType
from typing import Any, TypeVar, Union, cast, get_args, get_origin, get_type_hints

from pydantic import AliasChoices, BaseModel

ConfigT = TypeVar("ConfigT")
DataclassConfigT = TypeVar("DataclassConfigT")
PydanticConfigT = TypeVar("PydanticConfigT", bound=BaseModel)


def _declared_dataclass_type(
    owner_type: type[Any],
    field_name: str,
    default_type: type[DataclassConfigT],
) -> type[DataclassConfigT]:
    try:
        annotation = get_type_hints(owner_type).get(field_name)
    except (NameError, TypeError):
        return default_type

    candidates = (
        get_args(annotation) if get_origin(annotation) in (Union, UnionType) else (annotation,)
    )
    for candidate in candidates:
        if (
            isinstance(candidate, type)
            and is_dataclass(candidate)
            and issubclass(candidate, default_type)
        ):
            return candidate
    return default_type


def _dataclass_input_values(
    value: dict[str, Any],
    config_type: type[Any],
) -> dict[str, Any]:
    field_names = {config_field.name for config_field in fields(config_type)}
    return {name: field_value for name, field_value in value.items() if name in field_names}


def coerce_dataclass_config(
    value: ConfigT | dict[str, Any],
    config_type: type[ConfigT],
    *,
    parameter_name: str,
) -> ConfigT:
    """Normalize an SDK-owned dataclass configuration at its public input boundary."""
    if isinstance(value, config_type):
        return value
    if not isinstance(value, dict):
        raise TypeError(
            f"{parameter_name} must be a {config_type.__name__} instance or a dict, "
            f"got {type(value).__name__}"
        )

    field_names = {
        config_field.name for config_field in fields(cast(Any, config_type)) if config_field.init
    }
    unknown_fields = sorted(str(name) for name in value if name not in field_names)
    if unknown_fields:
        raise TypeError(f"Unknown {parameter_name} settings: {', '.join(unknown_fields)}")
    return config_type(**value)


def coerce_pydantic_config(
    value: PydanticConfigT | dict[str, Any],
    config_type: type[PydanticConfigT],
    *,
    parameter_name: str,
) -> PydanticConfigT:
    """Normalize an SDK-owned Pydantic configuration using its declared extra policy."""
    if isinstance(value, config_type):
        return value
    if not isinstance(value, dict):
        raise TypeError(
            f"{parameter_name} must be a {config_type.__name__} instance or a dict, "
            f"got {type(value).__name__}"
        )

    if config_type.model_config.get("extra") != "allow":
        accepted_fields: set[str] = set(config_type.model_fields)
        for field_info in config_type.model_fields.values():
            if isinstance(field_info.validation_alias, str):
                accepted_fields.add(field_info.validation_alias)
            elif isinstance(field_info.validation_alias, AliasChoices):
                accepted_fields.update(
                    alias for alias in field_info.validation_alias.choices if isinstance(alias, str)
                )
        unknown_fields = sorted(str(name) for name in value if name not in accepted_fields)
        if unknown_fields:
            raise TypeError(f"Unknown {parameter_name} settings: {', '.join(unknown_fields)}")

    return config_type.model_validate(value)
