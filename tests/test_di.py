"""Unit tests for ``fusionserve.di`` pure helpers."""

from __future__ import annotations

from fusionserve.di import _make_hashable


def test_make_hashable_primitives_are_returned_as_is():
    assert _make_hashable(1) == 1
    assert _make_hashable("a") == "a"
    assert _make_hashable(True) is True
    assert _make_hashable(None) is None
    assert _make_hashable(1.5) == 1.5


def test_make_hashable_dict_is_sorted_tuple_of_pairs():
    result = _make_hashable({"b": 2, "a": 1})
    assert result == (("a", 1), ("b", 2))
    # Re-running on a dict with reversed insertion order produces the same key.
    assert _make_hashable({"a": 1, "b": 2}) == result


def test_make_hashable_set_is_sorted_tuple():
    assert _make_hashable({3, 1, 2}) == (1, 2, 3)


def test_make_hashable_list_is_sorted_tuple():
    assert _make_hashable([3, 1, 2]) == (1, 2, 3)


def test_make_hashable_nested_dict_recurses():
    nested = {"outer": {"b": 2, "a": 1}}
    assert _make_hashable(nested) == (("outer", (("a", 1), ("b", 2))),)


def test_make_hashable_unknown_type_falls_back_to_str():
    class Marker:
        def __str__(self) -> str:
            return "marker-instance"

    assert _make_hashable(Marker()) == "marker-instance"


def test_make_hashable_output_is_actually_hashable():
    sample = {"x": [1, {"y": 2}, "z"], "w": True}
    hash(_make_hashable(sample))  # must not raise
