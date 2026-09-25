import os
from pathlib import Path

import pytest

from gpsrtk.io import read_any
from gpsrtk.site import example_site

ZIP = Path(__file__).resolve().parents[1] / "archive" / "Project 1.zip"


def pytest_collection_modifyitems(config, items):
    """Network tests stay off unless they are asked for.

    pyproject's `addopts` deselects them with `-m 'not network'`, but any
    `-m` on the command line replaces that - so `-m "not browser"` quietly
    selected every test that hits a live service. They run only when the
    `-m` expression names `network`, or YARDSURVEY_NETWORK=1 is set.
    """
    if "network" in (config.getoption("markexpr") or ""):
        return
    if os.environ.get("YARDSURVEY_NETWORK") == "1":
        return
    skip = pytest.mark.skip(reason="network tests are opt-in: -m network, "
                                   "or YARDSURVEY_NETWORK=1")
    for item in items:
        if item.get_closest_marker("network") is not None:
            item.add_marker(skip)


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
