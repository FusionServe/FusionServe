"""Dependency-injection helpers — thin re-export of upstream advanced_alchemy.

Until 2026-04 this module carried a verbatim copy of
``advanced_alchemy.extensions.litestar.providers``. The fork has been retired:
the upstream API (``create_filter_dependencies``, ``FilterConfig`` etc.)
covers our needs, so importing the symbols directly removes ~700 lines of
vendored code that needed to be kept in sync manually.

Anything that previously imported from ``fusionserve.di`` continues to work,
because the names below are the same identifiers re-exported from upstream.
"""

from __future__ import annotations

from advanced_alchemy.extensions.litestar.providers import (
    DEPENDENCY_DEFAULTS,
    DependencyCache,
    DependencyDefaults,
    FieldNameType,
    FilterConfig,
    create_filter_dependencies,
    create_service_dependencies,
    create_service_provider,
)

__all__ = [
    "DEPENDENCY_DEFAULTS",
    "DependencyCache",
    "DependencyDefaults",
    "FieldNameType",
    "FilterConfig",
    "create_filter_dependencies",
    "create_service_dependencies",
    "create_service_provider",
]
