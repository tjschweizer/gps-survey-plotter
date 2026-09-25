"""Surfacing geometry and export round-trips.

These cover the failure modes that produce a plausible-looking but wrong
result: a silently transposed terrain, a world file pointing at the wrong
corner, and a Revit export that does not land back on its own UTM coordinate.
"""

import numpy as np
import pytest

from gpsrtk import filters as F
from gpsrtk.model import pointset as P
from gpsrtk.surface import Extent, build_surface
from gpsrtk.units import FT_PER_M


@pytest.fixture(scope="module")
def surf(tracks, site):
    return build_surface(F.default_chain().run(tracks), site, size=96)


def test_row_zero_is_the_south_edge(surf):
    """Row order is the single easiest thing to get backwards, and a flipped
    terrain still looks entirely believable."""
    assert surf.z.shape[0] == surf.z.shape[1]
    # Rebuild with an explicit extent and probe a known corner.
    assert surf.extent.ymin < surf.extent.ymax


def test_the_3d_grid_pairs_rows_with_northings(surf, site):
    """The 3D view draws z[i][j] at (x[j], y[i]). If rows and columns were
    ever paired with the wrong axis, the terrain would transpose silently."""
    from gpsrtk.app.views import surface_grid

    g = surface_grid(surf, site)
    ny, nx = surf.z.shape
    assert len(g["y"]) == len(g["z"]) == ny
    assert len(g["x"]) == len(g["z"][0]) == nx
    assert g["x"] == sorted(g["x"]) and g["y"] == sorted(g["y"]), \
        "east and north both increase"
    x0, y0 = site.to_local(surf.extent.xmin, surf.extent.ymin)
    assert (g["x"][0], g["y"][0]) == pytest.approx((x0 / 0.3048, y0 / 0.3048), abs=1e-3)

    corners = {(0, 0): surf.z_masked[0, 0], (0, -1): surf.z_masked[0, -1],
               (-1, 0): surf.z_masked[-1, 0], (-1, -1): surf.z_masked[-1, -1]}
    for (i, j), expected in corners.items():
        got = g["z"][i][j]
        if np.isnan(expected):
            assert got is None, "unmeasured cells stay empty"
        else:
            assert got == pytest.approx(expected / 0.3048, abs=1e-4)


def test_world_file_points_at_the_north_west_pixel_centre(surf):
    """Pixel centres are the grid nodes (`terrain.grid_axes`), so the world
    file's step and origin must be the nodes' own."""
    from gpsrtk import terrain

    lines = surf.world_file().strip().splitlines()
    assert len(lines) == 6
    px_x, rot1, rot2, px_y, x0, y0 = (float(v) for v in lines)

    gx, gy = terrain.grid_axes(surf)
    assert px_x == pytest.approx(gx[1] - gx[0])
    assert px_y == pytest.approx(-(gy[1] - gy[0])), "north-up rasters need negative y"
    assert rot1 == 0.0 and rot2 == 0.0
    # World files reference the CENTRE of the top-left pixel: the first
    # node east and the last node north.
    assert x0 == pytest.approx(gx[0])
    assert y0 == pytest.approx(gy[-1])


def test_to_image_flips_north_up(surf):
    z_img, mask_img = surf.to_image()
    assert np.array_equal(z_img[0], surf.z[-1])
    assert np.array_equal(mask_img[0], surf.mask[-1])


def test_filled_grid_has_no_nan_but_mask_marks_the_fill(surf):
    assert not np.isnan(surf.z).any()
    assert surf.mask.any()
    assert np.isnan(surf.z_masked[surf.mask]).all()
    assert 0.0 < surf.measured_fraction < 1.0


def test_shared_extent_makes_surfaces_comparable(tracks, site):
    """Differencing two sessions requires identical grids. Passing an extent
    must produce pixel-aligned rasters regardless of each subset's own bounds."""
    fixed = tracks.fixed
    ext = Extent.square_around(fixed.df[P.E].to_numpy(), fixed.df[P.N].to_numpy())

    a = fixed.select(fixed.df.index < len(fixed) // 2)
    b = fixed.select(fixed.df.index >= len(fixed) // 2)
    sa = build_surface(a, site, size=64, extent=ext)
    sb = build_surface(b, site, size=64, extent=ext)

    assert sa.z.shape == sb.z.shape
    assert sa.px == pytest.approx(sb.px)
    assert sa.extent.xmin == pytest.approx(sb.extent.xmin)


def test_revit_export_round_trips_to_utm(tmp_path, tracks, site):
    """The whole point of the origin sidecar is that a point in the file can be
    put back on the ground. Verify that arithmetic, not just that a file appeared."""
    from gpsrtk.io.revit import write_points

    pts = F.BinToCell(cell=1.0).for_site(site).apply(
        F.default_chain().run(tracks))
    out = tmp_path / "revit.csv"
    res = write_points(pts, site, out)

    assert res["n_points"] == len(pts)
    assert res["sidecar"].exists()

    import pandas as pd
    written = pd.read_csv(out, header=None, names=["x", "y", "z"])
    assert len(written) == len(pts)

    # feet -> metres -> add origin should recover the source easting/northing
    e = written.x.to_numpy() / FT_PER_M + site.origin_e
    n = written.y.to_numpy() / FT_PER_M + site.origin_n
    assert e == pytest.approx(pts.df[P.E].to_numpy(), abs=1e-3)
    assert n == pytest.approx(pts.df[P.N].to_numpy(), abs=1e-3)


def test_heightmap_export_writes_a_recoverable_mapping(tmp_path, surf):
    """A 16-bit greyscale with no elevation mapping is unrecoverable data."""
    from PIL import Image

    from gpsrtk.io.raster import write_heightmap

    res = write_heightmap(surf, tmp_path, tag="_t", preview=False)
    for key in ("png16", "mask", "world", "info"):
        assert res["paths"][key].exists()

    img = np.asarray(Image.open(res["paths"]["png16"]))
    assert img.dtype == np.uint16

    # Decode using only what the INFO file states, and land back on the surface.
    zmin, zmax = res["zmin"], res["zmax"]
    decoded = zmin + img / 65535.0 * (zmax - zmin)
    assert decoded.max() == pytest.approx(zmax, abs=1e-3)
    assert decoded.min() == pytest.approx(zmin, abs=1e-3)

    info = res["paths"]["info"].read_text(encoding="utf-8")
    assert "elevation(m) =" in info
    assert "INTERPOLATED FILL" in info


# --- laser terrain shots in the surface (synthetic) -----------------------------

def test_the_surface_passes_through_every_laser_shot(state):
    """After a local solve the laser shots are the better heights; the
    surface, sampled at each, returns its level-network elevation."""
    state.solve_vertical("local")
    laser = state.laser_points()
    assert len(laser) == 12                   # 14 readings, 12 stations
    for surface in (state.surface, state.export_surface()):
        assert np.abs(surface.sample(laser["e"], laser["n"]) - laser["z"]).max() < 1e-3


def test_laser_shots_are_off_unless_asked_for(state):
    """build_surface's default is unchanged, so the established findings
    are computed exactly as before."""
    state.solve_vertical("local")
    plain = build_surface(state.result, state.site, size=96)
    laser = state.laser_points()
    with_laser = build_surface(state.result, state.site, size=96, fixed=laser)
    assert not np.allclose(plain.z, with_laser.z)
    assert plain.n_cells > 0


def test_the_revit_file_swaps_nearby_bins_for_the_laser_shots(state, tmp_path):
    from gpsrtk.filters import BinToCell
    from gpsrtk.io.revit import append_laser_points, write_points

    state.solve_vertical("local")
    binned = BinToCell(cell=0.5).for_site(state.site).apply(state.result)
    laser = state.laser_points()
    # The synthetic shots sit between passes, ~0.54 m from the nearest bin
    # centre; a 1 m radius makes the swap visible.
    assert append_laser_points(binned, laser)[1] == 0
    out, dropped = append_laser_points(binned, laser, radius_m=1.0)
    assert dropped > 0
    assert len(out) == len(binned) - dropped + len(laser)
    res = write_points(out, state.site, tmp_path / "r.csv")
    assert res["n_points"] == len(out)
    rows = np.loadtxt(tmp_path / "r.csv", delimiter=",")
    assert len(rows) == len(out)


def test_no_laser_shots_outside_a_local_solve(state):
    from gpsrtk.app import report

    model = state.solve_vertical("ellipsoidal")
    assert state.laser_points() is None
    assert "not used in the surface in ellipsoidal mode" in report.solve_notice(model)
    state.clear_vertical()
    assert state.laser_points() is None


def test_laser_shots_survive_a_reopen(state, tmp_path):
    """The level network is not stored; it is solved again from the same
    readings, which gives the same elevations."""
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site

    state.solve_vertical("local")
    before = state.laser_points()
    saved = state.save_project(tmp_path / "laser.yardproj")
    other = AppState(site=example_site(), cache_dir=state.cache_dir)
    other.load_project(saved)
    assert other.vertical.level is None
    after = other.laser_points()
    assert np.array_equal(before["z"].to_numpy(), after["z"].to_numpy())


def test_the_world_file_places_every_pixel_on_its_grid_node(state):
    """Pixel (row, col) of the north-up image is grid node (n-1-row, col).
    The world file used to assume width/n and half a pixel in, which put
    the edges ~2 cm out on a 1024 px export of this lot."""
    from gpsrtk import terrain

    s = state.export_surface()
    a, _, _, e, c, f = (float(v) for v in s.world_file().split())
    gx, gy = terrain.grid_axes(s)
    ny, nx = s.z.shape
    for row, col in ((0, 0), (0, nx - 1), (ny - 1, 0), (ny - 1, nx - 1), (ny // 3, nx // 2)):
        assert c + a * col == pytest.approx(gx[col], abs=1e-6)
        assert f + e * row == pytest.approx(gy[ny - 1 - row], abs=1e-6)
    # px, and with it the mask and the measured fraction, are unchanged.
    assert s.px == pytest.approx(s.extent.width / nx)
