"""Shared pytest fixtures.

The document fixtures are generated, not committed as binaries, so a reviewer can
read exactly what defect each one plants. Session scope keeps generation to one
pass per run.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.make_fixtures import build_all


@pytest.fixture(scope="session")
def fixtures() -> dict[str, Path]:
    """Every generated defect fixture, keyed by name.

    Returns:
        Mapping of fixture name to ``.docx`` path.
    """
    return build_all()


@pytest.fixture(scope="session")
def fixture_dir(fixtures: dict[str, Path]) -> Iterator[Path]:
    """The directory holding the fixtures.

    Yields:
        The fixtures directory.
    """
    yield next(iter(fixtures.values())).parent
