"""Tests for :func:`fusionserve.files.keys.make_storage_key`.

The key format is the only piece of contract clients see; pin its shape.
"""

from __future__ import annotations

import datetime
import re

from fusionserve.files.keys import make_storage_key


def test_make_storage_key_uses_iso_date_prefix():
    """The key prefix must be ``YYYY/MM/DD/`` derived from the timestamp."""
    fixed = datetime.datetime(2026, 3, 17, 12, 30, tzinfo=datetime.UTC)
    key = make_storage_key("text/plain", now=fixed)
    assert key.startswith("2026/03/17/")


def test_make_storage_key_picks_extension_from_mime():
    """Known MIME types must pick a recognisable extension."""
    fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    key = make_storage_key("image/png", now=fixed)
    # mimetypes.guess_extension("image/png") returns ".png"
    assert key.endswith(".png")


def test_make_storage_key_falls_back_to_bin_for_unknown_mime():
    """Unknown MIME types must fall back to ``.bin``."""
    fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    key = make_storage_key("application/x-unrecognised", now=fixed)
    assert key.endswith(".bin")


def test_make_storage_key_uses_uuid_basename():
    """The filename portion must be a 32-char hex UUID."""
    fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    key = make_storage_key("text/plain", now=fixed)
    basename = key.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    assert re.fullmatch(r"[0-9a-f]{32}", basename), f"unexpected basename {basename!r}"


def test_make_storage_key_is_unique_across_calls():
    """Two calls with the same inputs must produce different keys."""
    fixed = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    a = make_storage_key("text/plain", now=fixed)
    b = make_storage_key("text/plain", now=fixed)
    assert a != b
