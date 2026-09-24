from pathlib import Path

import pytest

from gpsrtk.io import read_any
from gpsrtk.site import example_site

ZIP = Path(__file__).resolve().parents[1] / "archive" / "Project 1.zip"


@pytest.fixture(scope="session")
def export():
    if not ZIP.exists():
        pytest.skip(f"reference export not present: {ZIP}")
    return read_any(ZIP)


@pytest.fixture(scope="session")
def site():
    return example_site()


@pytest.fixture(scope="session")
def tracks(export):
    return export["track_points"]


@pytest.fixture(scope="session")
def spots(export):
    return export["spots"]
