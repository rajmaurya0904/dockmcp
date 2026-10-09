"""Smoke test: package imports cleanly. Replace/extend as modules land."""

import dockmcp


def test_version_is_set() -> None:
    assert dockmcp.__version__
