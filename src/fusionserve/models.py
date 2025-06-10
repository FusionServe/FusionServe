from typing import Any

from pydantic import BaseModel, Field

from .config import settings


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
    limit: int = Field(100, alias="__limit", gt=0, le=settings.max_page_lenght)
    offset: int = Field(0, alias="__offset", ge=0)
    order_by: str | None = Field(None, alias="__order_by")
