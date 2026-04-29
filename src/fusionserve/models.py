import datetime
import uuid
from enum import Enum
from typing import Any

import strawberry
from pydantic import BaseModel, Field

from .config import settings


class ResolverType(Enum):
    """Types of GraphQL resolvers that can be generated for a table."""

    LIST = "list"
    PK = "pk"
    CREATE = "create"
    CREATE_MANY = "create_many"
    UPDATE = "update"
    UPDATE_MANY = "update_many"
    DELETE = "delete"
    DELETE_MANY = "delete_many"


@strawberry.enum
class SortDirection(Enum):
    """Sort direction options for GraphQL order_by arguments.

    Supports all combinations of ascending/descending with nulls
    first/last positioning.
    """

    ASC = "ASC"
    ASC_NULLS_FIRST = "ASC_NULLS_FIRST"
    ASC_NULLS_LAST = "ASC_NULLS_LAST"
    DESC = "DESC"
    DESC_NULLS_FIRST = "DESC_NULLS_FIRST"
    DESC_NULLS_LAST = "DESC_NULLS_LAST"


@strawberry.input(description="Comparison operators for String columns.")
class StringComparisonExp:
    """String comparison operators including pattern matching."""

    eq: str | None = strawberry.UNSET
    neq: str | None = strawberry.UNSET
    gt: str | None = strawberry.UNSET
    gte: str | None = strawberry.UNSET
    lt: str | None = strawberry.UNSET
    lte: str | None = strawberry.UNSET
    in_list: list[str] | None = strawberry.UNSET
    not_in_list: list[str] | None = strawberry.UNSET
    like: str | None = strawberry.UNSET
    ilike: str | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Int columns.")
class IntComparisonExp:
    """Integer comparison operators."""

    eq: int | None = strawberry.UNSET
    neq: int | None = strawberry.UNSET
    gt: int | None = strawberry.UNSET
    gte: int | None = strawberry.UNSET
    lt: int | None = strawberry.UNSET
    lte: int | None = strawberry.UNSET
    in_list: list[int] | None = strawberry.UNSET
    not_in_list: list[int] | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Float columns.")
class FloatComparisonExp:
    """Float comparison operators."""

    eq: float | None = strawberry.UNSET
    neq: float | None = strawberry.UNSET
    gt: float | None = strawberry.UNSET
    gte: float | None = strawberry.UNSET
    lt: float | None = strawberry.UNSET
    lte: float | None = strawberry.UNSET
    in_list: list[float] | None = strawberry.UNSET
    not_in_list: list[float] | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Boolean columns.")
class BooleanComparisonExp:
    """Boolean comparison operators (only eq and is_null)."""

    eq: bool | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for DateTime columns.")
class DateTimeComparisonExp:
    """DateTime comparison operators."""

    eq: datetime.datetime | None = strawberry.UNSET
    neq: datetime.datetime | None = strawberry.UNSET
    gt: datetime.datetime | None = strawberry.UNSET
    gte: datetime.datetime | None = strawberry.UNSET
    lt: datetime.datetime | None = strawberry.UNSET
    lte: datetime.datetime | None = strawberry.UNSET
    in_list: list[datetime.datetime] | None = strawberry.UNSET
    not_in_list: list[datetime.datetime] | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Date columns.")
class DateComparisonExp:
    """Date comparison operators."""

    eq: datetime.date | None = strawberry.UNSET
    neq: datetime.date | None = strawberry.UNSET
    gt: datetime.date | None = strawberry.UNSET
    gte: datetime.date | None = strawberry.UNSET
    lt: datetime.date | None = strawberry.UNSET
    lte: datetime.date | None = strawberry.UNSET
    in_list: list[datetime.date] | None = strawberry.UNSET
    not_in_list: list[datetime.date] | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for UUID columns.")
class UUIDComparisonExp:
    """UUID comparison operators (no ordering operators)."""

    eq: uuid.UUID | None = strawberry.UNSET
    neq: uuid.UUID | None = strawberry.UNSET
    gt: uuid.UUID | None = strawberry.UNSET
    gte: uuid.UUID | None = strawberry.UNSET
    lt: uuid.UUID | None = strawberry.UNSET
    lte: uuid.UUID | None = strawberry.UNSET
    in_list: list[uuid.UUID] | None = strawberry.UNSET
    not_in_list: list[uuid.UUID] | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET


COMPARISON_TYPE_MAP: dict[type, type] = {
    str: StringComparisonExp,
    int: IntComparisonExp,
    float: FloatComparisonExp,
    bool: BooleanComparisonExp,
    datetime.datetime: DateTimeComparisonExp,
    datetime.date: DateComparisonExp,
    uuid.UUID: UUIDComparisonExp,
}


# TODO: review validation pattern
pattern = r"^\(?\s*([a-zA-Z_]+)\s+(eq|ne|gt|ge|lt|le)\s+"
pattern += r"('[^']*'|\d+(\.\d+)?)\s*(\s+(and|or)\s+"
pattern += r"\(?\s*([a-zA-Z_]+)\s+(eq|ne|gt|ge|lt|le)\s+"
pattern += r"('[^']*'|\d+(\.\d+)?)\s*\)?\s*)*$"


class AdvancedFilter(BaseModel):
    filter: str | None = Field(
        None,
        alias="_filter",
        description="advanced **filter** on multiple fields using expressions",
        examples=["(author eq 'Kafka' or name eq 'Mike') and price lt 2.55"],
        pattern=pattern,
    )


class PaginationParams(BaseModel):
    limit: int = Field(100, alias="__limit", gt=0, le=settings.max_page_size)
    offset: int = Field(0, alias="__offset", ge=0)
    order_by: str | None = Field(None, alias="__order_by")


class SmartComment(BaseModel):
    metadata: dict[str, Any] | None = None
    content: str | None = None


class RecordNotFoundError(Exception):
    """Raised when a resolver cannot find a record by primary key.

    Used by GraphQL resolvers (PK lookup, update, delete) so callers see
    a typed, message-bearing error rather than a bare ``Exception``.
    """
