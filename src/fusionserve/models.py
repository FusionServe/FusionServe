import datetime
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from enum import Enum, StrEnum
from typing import Any, Literal

import strawberry
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Column, Table
from sqlalchemy.ext.automap import AutomapBase

from .config import settings

# Compiled once at module level; reusing compiled objects avoids per-call overhead.
_FRONTMATTER_PATTERN = re.compile(r"^---\s*$.*^---\s*$.*", re.MULTILINE | re.DOTALL)
_FRONTMATTER_BOUNDARY = re.compile(r"^---\s*$", re.MULTILINE)


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


# ---------------------------------------------------------------------------
# strawberry-orm filter-lookup overrides
# ---------------------------------------------------------------------------
#
# strawberry-orm ships date/time lookup inputs whose operator fields are typed
# ``str`` (``strawberry_orm.filters.DateTimeComparisonLookup`` et al.) and maps
# PostgreSQL ``uuid`` columns to ``str`` as well. Because the asyncpg dialect
# renders explicit bind casts (``$1::TYPE``) and SQLAlchemy derives a bind
# parameter's type from the *Python value* (``TypeEngine.coerce_compared_value``),
# a ``str`` value compared against a ``timestamp``/``uuid`` column emits
# ``col >= $1::VARCHAR`` and PostgreSQL rejects it with e.g.
# ``operator does not exist: timestamp with time zone >= character varying``.
#
# The lookups below mirror the field *names* the SQLAlchemy backend's
# ``_build_lookup_clauses`` recognises (``exact``/``neq``/``gt``/``gte``/``lt``/
# ``lte``/``in_list``/``not_in_list``/``is_null``/``range``) but type the value
# fields concretely, so Strawberry coerces the GraphQL input into real
# ``datetime``/``date``/``time``/``uuid.UUID`` objects. SQLAlchemy then keeps the
# column type and renders ``$1::TIMESTAMP``/``$1::UUID``. They are wired in via
# the backend's public ``filter_overrides`` hook (see :data:`FILTER_OVERRIDES`
# and ``fusionserve.graphql.build``).


@strawberry.input(description="Datetime range (inclusive) for `range` lookups.")
class DateTimeRangeInput:
    """Inclusive ``[start, end]`` bounds for a datetime ``range`` lookup."""

    start: datetime.datetime
    end: datetime.datetime


@strawberry.input(description="Date range (inclusive) for `range` lookups.")
class DateRangeInput:
    """Inclusive ``[start, end]`` bounds for a date ``range`` lookup."""

    start: datetime.date
    end: datetime.date


@strawberry.input(description="Time range (inclusive) for `range` lookups.")
class TimeRangeInput:
    """Inclusive ``[start, end]`` bounds for a time ``range`` lookup."""

    start: datetime.time
    end: datetime.time


@strawberry.input(description="Comparison operators for DateTime columns.")
class DateTimeComparisonLookup:
    """DateTime lookups typed as :class:`datetime.datetime` (not ``str``)."""

    exact: datetime.datetime | None = strawberry.UNSET
    neq: datetime.datetime | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[datetime.datetime] | None = strawberry.UNSET
    not_in_list: list[datetime.datetime] | None = strawberry.UNSET
    gt: datetime.datetime | None = strawberry.UNSET
    gte: datetime.datetime | None = strawberry.UNSET
    lt: datetime.datetime | None = strawberry.UNSET
    lte: datetime.datetime | None = strawberry.UNSET
    range: DateTimeRangeInput | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Date columns.")
class DateComparisonLookup:
    """Date lookups typed as :class:`datetime.date` (not ``str``)."""

    exact: datetime.date | None = strawberry.UNSET
    neq: datetime.date | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[datetime.date] | None = strawberry.UNSET
    not_in_list: list[datetime.date] | None = strawberry.UNSET
    gt: datetime.date | None = strawberry.UNSET
    gte: datetime.date | None = strawberry.UNSET
    lt: datetime.date | None = strawberry.UNSET
    lte: datetime.date | None = strawberry.UNSET
    range: DateRangeInput | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for Time columns.")
class TimeComparisonLookup:
    """Time lookups typed as :class:`datetime.time` (not ``str``)."""

    exact: datetime.time | None = strawberry.UNSET
    neq: datetime.time | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    gt: datetime.time | None = strawberry.UNSET
    gte: datetime.time | None = strawberry.UNSET
    lt: datetime.time | None = strawberry.UNSET
    lte: datetime.time | None = strawberry.UNSET
    range: TimeRangeInput | None = strawberry.UNSET


@strawberry.input(description="Comparison operators for UUID columns.")
class UUIDComparisonLookup:
    """UUID lookups typed as :class:`uuid.UUID` (not ``str``); no ordering."""

    exact: uuid.UUID | None = strawberry.UNSET
    neq: uuid.UUID | None = strawberry.UNSET
    is_null: bool | None = strawberry.UNSET
    in_list: list[uuid.UUID] | None = strawberry.UNSET
    not_in_list: list[uuid.UUID] | None = strawberry.UNSET


#: Concrete-typed lookup overrides wired into the strawberry-orm SQLAlchemy
#: backend via its ``filter_overrides`` hook. Keyed on the column's introspected
#: Python type. ``uuid.UUID`` only takes effect once non-PK uuid columns
#: introspect as ``uuid.UUID`` (see the ``_SA_TYPE_MAP`` patch in
#: ``fusionserve.graphql``); uuid primary keys / FKs route through the backend's
#: ``ReferenceLookup`` instead and are handled by the ``_coerce_reference_value``
#: patch there.
FILTER_OVERRIDES: dict[type, type] = {
    datetime.datetime: DateTimeComparisonLookup,
    datetime.date: DateComparisonLookup,
    datetime.time: TimeComparisonLookup,
    uuid.UUID: UUIDComparisonLookup,
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


class SmartCommentMetadata(BaseModel):
    """Validated YAML-frontmatter payload of a smart comment.

    Known keys are typed and validated; any other keys are ignored
    (``extra="ignore"``) and dropped during parsing.

    Attributes:
        primary_key: Logical primary-key column name(s) used to map a view
            (which carries no primary key in the PostgreSQL catalogue). A bare
            string is coerced to a single-element list. ``None`` when the
            comment does not declare one.
    """

    model_config = ConfigDict(extra="ignore")

    primary_key: list[str] | None = None

    @field_validator("primary_key", mode="before")
    @classmethod
    def _coerce_primary_key(cls, value: Any) -> Any:
        """Coerce a bare string to a one-element list and reject blank entries.

        Args:
            value: The raw ``primary_key`` value from the parsed frontmatter.

        Returns:
            ``None`` unchanged, a single string wrapped in a list, or the
            original list with each entry stripped.

        Raises:
            ValueError: If any entry is not a non-empty string.
        """
        if value is None:
            return None
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not items:
            raise ValueError("primary_key must be a non-empty string or list of strings")
        cleaned: list[str] = []
        for item in items:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("primary_key entries must be non-empty strings")
            cleaned.append(item.strip())
        return cleaned


class SmartComment(BaseModel):
    """Parsed table / column / function comment, optionally with YAML frontmatter.

    Attributes:
        metadata: Validated YAML-frontmatter payload, when present.
        content: Plain-text body following the (optional) frontmatter block.
    """

    metadata: SmartCommentMetadata | None = None
    content: str | None = None

    @classmethod
    def from_text(cls, comment: str | None) -> SmartComment:
        """Parse a raw comment string, extracting optional YAML frontmatter.

        If the input starts with a YAML frontmatter block delimited by ``---``
        markers, the metadata is parsed, validated against
        :class:`SmartCommentMetadata`, and returned alongside the plain-text
        content. Any YAML *parse* error falls back to returning the whole input
        as plain-text content — no exception is raised in that case (per the
        parsing contract).

        Well-formed YAML that fails :class:`SmartCommentMetadata` validation
        (e.g. a malformed ``primary_key``) is treated as an authoring error and
        the resulting :class:`pydantic.ValidationError` is allowed to propagate.

        Args:
            comment: The raw comment string. ``None`` or empty input yields an
                empty :class:`SmartComment`.

        Returns:
            A :class:`SmartComment` with optional ``metadata`` and ``content``
            fields populated.

        Raises:
            pydantic.ValidationError: If the parsed frontmatter is well-formed
                YAML but does not satisfy :class:`SmartCommentMetadata`.
        """
        if not comment:
            return cls()

        if not _FRONTMATTER_PATTERN.fullmatch(comment):
            return cls(content=comment)

        _, frontmatter, content = _FRONTMATTER_BOUNDARY.split(comment, 2)

        try:
            metadata = yaml.safe_load(frontmatter)
        except yaml.YAMLError:
            return cls(content=comment)

        return cls(metadata=SmartCommentMetadata.model_validate(metadata), content=content.lstrip("\n"))

    @classmethod
    def from_object(cls, obj: Table | Column) -> SmartComment:
        """Parse the ``.comment`` attribute of a SQLAlchemy ``Table`` or ``Column``.

        Thin adapter around :meth:`from_text` for SQLAlchemy ``Table`` and
        ``Column`` objects (both expose ``.comment``).

        Args:
            obj: SQLAlchemy ``Table`` or ``Column`` whose ``comment`` attribute
                is parsed.

        Returns:
            A :class:`SmartComment` with optional ``metadata`` and ``content``
            fields populated.
        """
        return cls.from_text(obj.comment)


class RecordNotFoundError(Exception):
    """Raised when a resolver cannot find a record by primary key.

    Used by GraphQL resolvers (PK lookup, update, delete) so callers see
    a typed, message-bearing error rather than a bare ``Exception``.
    """


class FunctionVolatility(StrEnum):
    """PostgreSQL function volatility classifications we expose as queries.

    Only ``STABLE`` and ``IMMUTABLE`` functions are eligible for the custom-query
    feature; ``VOLATILE`` functions are deliberately excluded because they may
    have side effects and conceptually belong on the ``Mutation`` root.
    """

    STABLE = "s"
    IMMUTABLE = "i"


class FunctionReturnKind(StrEnum):
    """The shape of a custom-query function's return value.

    Attributes:
        SCALAR: Function returns a single mapped Python scalar.
        ROW: Function returns a single row of an existing table type.
        SET: Function returns ``SETOF`` an existing table type (zero or more rows).
    """

    SCALAR = "scalar"
    ROW = "row"
    SET = "set"


class FunctionSkipReason(StrEnum):
    """Why a discovered PG function cannot be exposed as a custom query.

    Attributes:
        NON_IN_ARG_MODE: One or more parameters use OUT/INOUT/VARIADIC/TABLE
            modes; only pure IN parameters are supported in v1.
        UNNAMED_ARG: One or more parameters are positional-only (no entry in
            ``proargnames``); custom queries require named parameters so the
            GraphQL/REST surfaces have argument names to expose.
        UNSUPPORTED_ARG_TYPE: At least one parameter's PostgreSQL type is not
            in the supported scalar map.
        UNSUPPORTED_RETURN: The return type is neither a supported scalar nor
            a known reflected table (single row or SETOF).
    """

    NON_IN_ARG_MODE = "non_in_arg_mode"
    UNNAMED_ARG = "unnamed_arg"
    UNSUPPORTED_ARG_TYPE = "unsupported_arg_type"
    UNSUPPORTED_RETURN = "unsupported_return"


@dataclass(frozen=True)
class FunctionSkip:
    """Sentinel returned from :meth:`PgFunctionInfo.to_function_info`.

    Carries enough context for the caller to log a useful warning. The model
    method itself never logs — separation of policy (the loader logs) from
    classification (the model decides) keeps both halves easy to test.

    Attributes:
        reason: Categorical reason for the skip.
        message: Human-readable detail (already formatted, ready for logging).
    """

    reason: FunctionSkipReason
    message: str


@dataclass
class FunctionParam:
    """One IN parameter of a custom-query function.

    Attributes:
        name: The parameter name as declared in PostgreSQL (snake_case).
        pg_type: The PostgreSQL type name (e.g. ``int4``, ``uuid``).
        python_type: The Python / Strawberry type the parameter is mapped to.
        has_default: ``True`` if the function declares a PG-side default for
            this parameter; the GraphQL / REST argument is optional in that
            case so the default is honoured.
    """

    name: str
    pg_type: str
    python_type: type
    has_default: bool


@dataclass
class FunctionInfo:
    """Metadata for one custom-query PostgreSQL function.

    Populated by :func:`fusionserve.persistence._introspect_functions` and
    consumed by both the GraphQL builder and the REST controller builder.

    Attributes:
        schema: The PostgreSQL schema the function lives in.
        name: The function name as declared in PostgreSQL (snake_case).
        volatility: ``STABLE`` or ``IMMUTABLE``.
        params: Ordered list of IN parameters.
        return_kind: Whether the function returns a scalar, a row, or a set.
        return_pg_type: PostgreSQL type name of the return.
        return_python_type: Mapped Python type for ``SCALAR`` returns; ``None``
            when the return is a row or a set of a known table.
        return_table_name: Table name when the return is a row or set of an
            existing reflected table; ``None`` otherwise.
        description: Plain-text portion of the function's smart comment.
        metadata: Validated YAML-frontmatter portion of the function's smart
            comment (currently stored for future authorization hooks; unused
            at v1).
    """

    schema: str
    name: str
    volatility: FunctionVolatility
    params: list[FunctionParam]
    return_kind: FunctionReturnKind
    return_pg_type: str
    return_python_type: type | None = None
    return_table_name: str | None = None
    description: str | None = None
    metadata: SmartCommentMetadata | None = None


@dataclass
class Introspection:
    """Result of one schema-introspection pass.

    Bundles the SQLAlchemy automap base with the discovered custom-query
    functions so callers (REST + GraphQL builders) can be wired off a single
    object.

    Attributes:
        base: The SQLAlchemy automap ``Base`` whose ``.classes`` attribute
            maps reflected table names to ORM classes.
        functions: List of :class:`FunctionInfo` entries — one per supported
            ``STABLE`` / ``IMMUTABLE`` function discovered in
            ``settings.pg_app_schema``.
        views: Names of mapped classes that are backed by (materialized or
            plain) views. These are exposed read-only — no create/update/delete
            surface is generated for them.
    """

    base: AutomapBase
    functions: list[FunctionInfo] = dc_field(default_factory=list)
    views: set[str] = dc_field(default_factory=set)

    # immutable, stable, volatile


ProKind = Literal["f", "a", "w", "p"]  # function, aggregate, window, procedure
TypType = Literal["b", "c", "d", "e", "p", "r", "m"]
# base, composite, domain, enum,
# pseudo, range, multirange
ArgMode = Literal["i", "o", "b", "v", "t"]  # in, out, inout, variadic, table


class PgFunctionInfo(BaseModel):
    """A row from a pg_proc + pg_type + pg_description introspection query."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        str_strip_whitespace=True,
    )
    # Identity
    oid: int = Field(ge=0)
    proname: str = Field(min_length=1)
    # Behavior flags
    provolatile: FunctionVolatility
    prokind: ProKind
    proretset: bool
    # Argument shape
    pronargs: int = Field(ge=0)
    pronargdefaults: int = Field(ge=0)
    proargnames: list[str] | None = None
    proargmodes: list[ArgMode] | None = None
    arg_typnames: list[str]
    # Return shape
    return_typname: str = Field(min_length=1)
    return_typtype: TypType
    return_typrelid: int = Field(ge=0)
    return_relname: str | None = None
    # Description
    comment: str | None = None

    def to_function_info(
        self,
        schema: str,
        known_tables: set[str],
        pg_to_py: Mapping[str, type],
    ) -> FunctionInfo | FunctionSkip:
        """Project this raw PG row into a validated domain :class:`FunctionInfo`.

        Validates argument modes (IN-only), argument naming, argument types
        against ``pg_to_py``, and the return classification (scalar / known
        table row / SETOF known table). Smart-comment parsing happens here
        too so the produced :class:`FunctionInfo` is fully populated.

        Overload detection is **not** performed here because it is not
        observable from a single row — the caller groups by ``proname`` and
        elides duplicates before invoking this method.

        Args:
            schema: The schema the function lives in. Carried through to the
                produced :class:`FunctionInfo`; not derived from this row.
            known_tables: Names of tables already reflected by automap; used
                to validate ROW / SET return types.
            pg_to_py: Map from ``pg_type.typname`` to the Python / Strawberry
                type to expose. Passed in (rather than imported) so the model
                stays decoupled from any particular type-map source and can
                be unit-tested with a fake.

        Returns:
            A populated :class:`FunctionInfo` on success, or a
            :class:`FunctionSkip` describing why this row cannot be exposed.
        """
        # 1. Reject non-IN argument modes. ``proargmodes`` is NULL when every
        #    argument is IN.
        if self.proargmodes is not None and any(m != "i" for m in self.proargmodes[: self.pronargs]):
            return FunctionSkip(
                reason=FunctionSkipReason.NON_IN_ARG_MODE,
                message="only pure IN parameters are supported (found OUT/INOUT/VARIADIC/TABLE modes).",
            )
        # 2. Reject positional-only parameters (no ``proargnames`` array).
        if self.pronargs > 0 and not self.proargnames:
            return FunctionSkip(
                reason=FunctionSkipReason.UNNAMED_ARG,
                message="positional-only parameters are not supported. "
                "Declare named parameters in the function signature.",
            )
        # 3. Validate per-argument types and build domain ``FunctionParam``s.
        params: list[FunctionParam] = []
        for idx in range(self.pronargs):
            pg_type = self.arg_typnames[idx] if idx < len(self.arg_typnames) else None
            if pg_type is None or pg_type not in pg_to_py:
                return FunctionSkip(
                    reason=FunctionSkipReason.UNSUPPORTED_ARG_TYPE,
                    message=f"argument #{idx + 1} has unsupported PostgreSQL type {pg_type!r}.",
                )
            arg_name = (self.proargnames[idx] if self.proargnames and idx < len(self.proargnames) else "") or ""
            if not arg_name:
                return FunctionSkip(
                    reason=FunctionSkipReason.UNNAMED_ARG,
                    message=f"argument #{idx + 1} has no declared name.",
                )
            # PG-side defaults attach to the trailing ``pronargdefaults`` arguments.
            has_default = idx >= (self.pronargs - self.pronargdefaults)
            params.append(
                FunctionParam(
                    name=arg_name,
                    pg_type=pg_type,
                    python_type=pg_to_py[pg_type],
                    has_default=has_default,
                )
            )
        # 4. Classify the return.
        return_python_type: type | None = None
        return_table_name: str | None = None
        if self.return_relname and self.return_relname in known_tables:
            return_table_name = self.return_relname
            return_kind = FunctionReturnKind.SET if self.proretset else FunctionReturnKind.ROW
        elif not self.proretset and self.return_typname in pg_to_py:
            return_kind = FunctionReturnKind.SCALAR
            return_python_type = pg_to_py[self.return_typname]
        else:
            prefix = "SETOF " if self.proretset else ""
            return FunctionSkip(
                reason=FunctionSkipReason.UNSUPPORTED_RETURN,
                message=f"unsupported return type {prefix}{self.return_typname!r} "
                "(expected scalar, single row of a known table, or SETOF a known table).",
            )
        # 5. Smart-comment parsing.
        smart = SmartComment.from_text(self.comment)
        return FunctionInfo(
            schema=schema,
            name=self.proname,
            volatility=self.provolatile,
            params=params,
            return_kind=return_kind,
            return_pg_type=self.return_typname,
            return_python_type=return_python_type,
            return_table_name=return_table_name,
            description=smart.content,
            metadata=smart.metadata,
        )
