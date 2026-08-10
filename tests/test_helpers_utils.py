"""helpers/utils.py: the not_none narrowing helper and cube thumbnails.

not_none is shared typing infrastructure (CLAUDE.md's "Type Checking"
conventions tell new code to use it), so its contract is pinned here:
identity passthrough, and an explicit raise — NOT an `assert`, which
`python -O` would strip silently.
"""
import pytest

from helpers.utils import DEFAULT_THUMBNAIL, get_cube_thumbnail_url, not_none


def test_not_none_returns_the_value_unchanged():
    sentinel = object()
    assert not_none(sentinel) is sentinel
    assert not_none(0) == 0            # falsy values are NOT None
    assert not_none("") == ""


def test_not_none_raises_valueerror_on_none():
    with pytest.raises(ValueError):
        not_none(None)


def test_cube_thumbnail_falls_back_to_default():
    assert get_cube_thumbnail_url("NoSuchCube") == DEFAULT_THUMBNAIL
    assert get_cube_thumbnail_url(None) == DEFAULT_THUMBNAIL
    assert get_cube_thumbnail_url("LSVCube") != DEFAULT_THUMBNAIL
