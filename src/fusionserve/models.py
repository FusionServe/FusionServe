from enum import Enum
from typing import Any

import strawberry
from pydantic import BaseModel, Field

from .config import settings


class ResolverType(Enum):
    """Types of GraphQL resolvers that can be generated for a table."""

    LIST = "list"
    PK = "pk"


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


class RegistryItem(BaseModel):
    model: Any = None
    get_input: Any = None
    create_input: Any = None
    pk_input: Any = None


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
        examples="(author eq 'Kafka' or name eq 'Mike') and price lt 2.55",
        pattern=pattern,
    )


class PaginationParams(BaseModel):
    limit: int = Field(100, alias="__limit", gt=0, le=settings.max_page_length)
    offset: int = Field(0, alias="__offset", ge=0)
    order_by: str | None = Field(None, alias="__order_by")


class SmartComment(BaseModel):
    metadata: dict[str, Any] | None = None
    content: str | None = None
