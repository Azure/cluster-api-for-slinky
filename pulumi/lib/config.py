"""Pydantic helpers for JSON-like Pulumi config payloads."""

from __future__ import annotations

from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
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
