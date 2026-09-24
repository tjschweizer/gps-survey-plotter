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


# --- a synthetic export, for behaviour that any plausible data exercises ----

@pytest.fixture
def state(synthetic_zip, tmp_path):
    """An application state with the synthetic export loaded."""
    from gpsrtk.app import AppState

    st = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    st.load(synthetic_zip)
    return st



@pytest.fixture(scope="session")
def synthetic_zip(tmp_path_factory):
    """A small SW Maps export around the example site. See `synthetic.py`."""
    from synthetic import write_export

    return write_export(tmp_path_factory.mktemp("synthetic") / "Synthetic Yard.zip")


@pytest.fixture(scope="session")
def synthetic_outing2(tmp_path_factory):
    """The same ground a month later, on a mount 5 cm higher."""
    from synthetic import write_export

    return write_export(tmp_path_factory.mktemp("synthetic2") / "Outing 2.zip",
                        day="2026-09-27 13:00:00", dz=0.05, seed=1,
                        spots=False)


@pytest.fixture(scope="session")
def synthetic_elsewhere(tmp_path_factory):
    """An outing on ground no other outing covers."""
    from synthetic import write_export

    return write_export(tmp_path_factory.mktemp("synthetic3") / "Elsewhere.zip",
                        day="2026-10-02 10:00:00", de=200.0, seed=2,
                        spots=False)


@pytest.fixture(scope="session")
def tracks(export):
    return export["track_points"]


@pytest.fixture(scope="session")
def spots(export):
    return export["spots"]
