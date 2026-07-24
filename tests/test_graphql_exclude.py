"""Unit tests for smart-comment ``exclude`` gating on the GraphQL mutation surface.

Only the pure, DB-free gating paths are exercised here (the full schema build
needs a live PostgreSQL introspection, covered by the integration suite).
"""

from __future__ import annotations

from fusionserve.graphql import _attach_mutations
from fusionserve.models import CrudAction


def test_attach_mutations_returns_false_when_all_crud_excluded():
    class Mutation:
        pass

    # All of create/update/delete excluded -> early return before any ORM access,
    # so ``orm``/``orm_class``/``gql_type`` are never touched.
    result = _attach_mutations(Mutation, None, None, None, excluded=frozenset(CrudAction))

    assert result is False
    assert not [name for name in vars(Mutation) if not name.startswith("__")]
