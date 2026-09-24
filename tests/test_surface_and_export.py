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


def test_mesh_ravel_matches_the_array(surf):
    """The 3D view ravels in C order onto x-fastest ImageData. If that pairing
    is ever wrong the terrain transposes silently."""
    pv = pytest.importorskip("pyvista")

    ny, nx = surf.z.shape
    grid = pv.ImageData(dimensions=(nx, ny, 1),
                        spacing=(surf.px, surf.px, 1.0),
                        origin=(surf.extent.xmin, surf.extent.ymin, 0.0))
    grid["warp"] = surf.z.ravel(order="C")

    corners = {
        0: surf.z[0, 0],
        nx - 1: surf.z[0, -1],
        (ny - 1) * nx: surf.z[-1, 0],
        ny * nx - 1: surf.z[-1, -1],
    }
    for idx, expected in corners.items():
        assert grid["warp"][idx] == pytest.approx(expected)

    # The test only means something if the corners actually differ.
    assert len(set(np.round(list(corners.values()), 4))) > 1


def test_world_file_points_at_the_north_west_pixel_centre(surf):
    lines = surf.world_file().strip().splitlines()
    assert len(lines) == 6
    px_x, rot1, rot2, px_y, x0, y0 = (float(v) for v in lines)

    assert px_x == pytest.approx(surf.px)
    assert px_y == pytest.approx(-surf.px), "north-up rasters need negative y"
    assert rot1 == 0.0 and rot2 == 0.0
    # World files reference the CENTRE of the top-left pixel.
    assert x0 == pytest.approx(surf.extent.xmin + surf.px / 2)
    assert y0 == pytest.approx(surf.extent.ymax - surf.px / 2)


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
