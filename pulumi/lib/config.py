"""Pydantic helpers for JSON-like Pulumi config payloads."""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_serializer,
)
from pydantic.alias_generators import to_camel


# Reusable constrained string that rejects empty values. Prefer this over
# hand-written ``if not value`` field validators.
NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
StrictPositiveInt = Annotated[int, Field(gt=0, strict=True)]
StrictNonNegativeInt = Annotated[int, Field(ge=0, strict=True)]


class PulumiConfigModel(BaseModel):
    """Base model for Pulumi config objects serialized into JSON payloads."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    def to_config(self) -> dict[str, object]:
        return self.model_dump(
            by_alias=True,
            mode="json",
            exclude_none=True,
            exclude_defaults=True,
        )


class DisabledConfig(PulumiConfigModel):
    """Common disabled config branch for enabled/disabled discriminated unions."""

    enabled: Literal[False] = False

    @field_serializer("enabled")
    def serialize_enabled(self, enabled: bool) -> bool:
        return enabled


class EnabledConfig(PulumiConfigModel):
    """Common enabled config branch for enabled/disabled discriminated unions."""

    enabled: Literal[True] = True

    @field_serializer("enabled")
    def serialize_enabled(self, enabled: bool) -> bool:
        return enabled


class _MaybeDisabledMeta(type):
    def __instancecheck__(cls, instance: object) -> bool:
        enabled_config = getattr(cls, "__enabled_config__", None)
        return isinstance(enabled_config, type) and isinstance(instance, enabled_config)


class MaybeDisabled(metaclass=_MaybeDisabledMeta):
    """Constructible enabled/disabled discriminated union."""

    __enabled_config__: type[EnabledConfig]

    def __init__(self, value: object | None = None, **kwargs: object) -> None:
        pass

    def __class_getitem__(cls, enabled_config: type[EnabledConfig]) -> type[object]:
        return maybe_disabled(enabled_config)


def maybe_disabled(enabled_config: type[EnabledConfig]) -> type[MaybeDisabled]:
    """Decorate an enabled config class into a maybe-disabled config type."""

    union_type = Annotated[
        Union[DisabledConfig, enabled_config],
        Field(discriminator="enabled"),
    ]
    adapter = TypeAdapter(union_type)

    class _MaybeDisabled(MaybeDisabled):
        __enabled_config__ = enabled_config

        @classmethod
        def __get_pydantic_core_schema__(cls, source_type: object, handler: Any) -> Any:
            return handler.generate_schema(union_type)

        def __new__(cls, value: object | None = None, **kwargs: object) -> object:
            if value is None:
                value = kwargs
            elif kwargs:
                if not isinstance(value, dict):
                    raise TypeError("keyword overrides require a mapping input")
                value = {**value, **kwargs}
            return adapter.validate_python(value)

        def __init__(self, value: object | None = None, **kwargs: object) -> None:
            pass

    _MaybeDisabled.__name__ = enabled_config.__name__
    _MaybeDisabled.__qualname__ = enabled_config.__qualname__
    _MaybeDisabled.__module__ = enabled_config.__module__
    return _MaybeDisabled