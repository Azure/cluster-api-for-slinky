"""Helpers for typed Pulumi component output composites."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass


class CompositeOutput:
    """Dataclass base for grouped ComponentResource outputs."""

    def to_outputs(self) -> dict[str, object]:
        if not is_dataclass(self):
            raise TypeError("CompositeOutput subclasses must be dataclasses")

        return {
            field.name: _to_output_value(getattr(self, field.name))
            for field in fields(self)
        }


def _to_output_value(value: object) -> object:
    if isinstance(value, CompositeOutput):
        return value.to_outputs()
    if isinstance(value, Mapping):
        return {key: _to_output_value(nested) for key, nested in value.items()}
    if isinstance(value, list):
        return [_to_output_value(nested) for nested in value]
    if isinstance(value, tuple):
        return tuple(_to_output_value(nested) for nested in value)
    return value
