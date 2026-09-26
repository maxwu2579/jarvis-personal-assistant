"""Bounded JSON data only: never expressions, handlers or executable objects."""

import json
import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_serializer

from app.schemas.common import serialize_utc

MemoryCategory = Literal["preference", "stable_fact", "constraint"]
MemoryKey = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]{0,79}$")]
Scalar = str | int | float | bool


def validate_value(value: object) -> object:
    def scalar(item: object) -> bool:
        return type(item) in (str, int, bool) or (type(item) is float and math.isfinite(item))

    if scalar(value):
        pass
    elif type(value) is list:
        if len(value) > 64 or not all(scalar(item) for item in value):
            raise ValueError("Use at most 64 finite scalar list items; nesting is not allowed")
    elif type(value) is dict:
        if len(value) > 32 or not all(
            type(k) is str and 1 <= len(k) <= 80 and not any(ord(c) < 32 or ord(c) == 127 for c in k)
            and scalar(v) for k, v in value.items()
        ):
            raise ValueError("Use at most 32 named scalar fields; nesting is not allowed")
    else:
        raise ValueError("Value must be a finite scalar, scalar list or flat scalar object")
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8"))
    except (ValueError, UnicodeError, TypeError, OverflowError):
        raise ValueError("Value must be valid UTF-8 JSON") from None
    if size > 4096:
        raise ValueError("Serialized value must not exceed 4096 UTF-8 bytes")
    return value


MemoryValue = Annotated[Scalar | list[Scalar] | dict[str, Scalar], BeforeValidator(validate_value)]


class MemoryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    category: MemoryCategory
    key: MemoryKey
    value: MemoryValue


class MemoryUpdate(MemoryCreate):
    expected_version: int = Field(ge=1)


class MemoryOut(BaseModel):
    id: int
    category: MemoryCategory
    key: str
    value: MemoryValue
    source_type: Literal["manual"]
    version: int
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def timestamps(self, value: datetime) -> str:
        return serialize_utc(value)
