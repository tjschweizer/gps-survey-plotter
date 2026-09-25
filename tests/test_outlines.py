"""Outlines: buildings, beds, fences, the lot - and keep-out areas.

An outline is a closed run of numbered shots, so everything a shot already
does - the field sheet, the tape, the SW Maps "P12" fill - works on its
corners. What is new is what the outline itself means: a keep-out outline's
inside is not terrain, so it leaves the surface, the exports and the tie
transects. Browser behaviour (drawing it, picking it) is in
`test_web_browser.py`.
"""

import json

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gpsrtk.app import plan_edit as PE
from gpsrtk.plan import OUTLINE_KINDS, PURPOSES, Plan
from gpsrtk.surface import build_surface, inside_polygons
from gpsrtk.units import M_PER_FT

# The synthetic lot is 40 x 30 m east and north of this origin.
OE, ON = 449700.0, 4604550.0


def box(x0, y0, x1, y1):
    """Corners of a rectangle in local metres on the synthetic lot, projected,
    anticlockwise from the south-west."""
    return [(OE + x0, ON + y0), (OE + x1, ON + y0),
            (OE + x1, ON + y1), (OE + x0, ON + y1)]


@pytest.fixture
def plan():
    return Plan(epsg=32615, name="test")


# --- the model ------------------------------------------------------------------------

def test_an_outline_numbers_its_corners_as_shots_of_its_kind(plan):
    plan.add_point(OE, ON)                                   # 1: a loose shot
    house = plan.add_outline("house", box(10, 10, 20, 18))
    assert house.numbers == [2, 3, 4, 5]
    assert house.closed and house.is_outline and house.keep_out
    assert {plan.by_number(n).purpose for n in house.numbers} == {"building corner"}


def test_keep_out_follows_the_kind_unless_given(plan):
    assert plan.add_outline("drive", box(0, 0, 4, 10), "driveway").keep_out is False
    assert plan.add_outline("fence", box(0, 0, 4, 10), "fence").keep_out is False
    assert plan.add_outline("bed", box(0, 0, 4, 10), "landscaping").keep_out is True
    assert plan.add_outline("lot", box(0, 0, 40, 30), "property line",
                            keep_out=True).keep_out is True


def test_every_kind_shoots_its_corners_as_a_known_purpose():
    for kind, (purpose, _) in OUTLINE_KINDS.items():
        assert purpose in PURPOSES, kind


def test_an_outline_needs_three_corners_and_a_known_kind(plan):
    with pytest.raises(ValueError, match="three corners"):
        plan.add_outline("sliver", box(0, 0, 1, 1)[:2])
    with pytest.raises(ValueError, match="outline kind"):
        plan.add_outline("gazebo", box(0, 0, 1, 1), "gazebo")


def test_a_measured_corner_moves_the_keep_out_area(plan):
    """The click was a guess at the corner; the shot is where it is."""
    house = plan.add_outline("house", box(10, 10, 20, 18))
    corner = plan.by_number(house.numbers[2])
    corner.observed_e, corner.observed_n = OE + 21.0, ON + 19.0
    corner.method, corner.fix = "rtk", 4
    [(name, xy)] = plan.keep_out_areas()
    assert name == "house"
    assert xy[2] == pytest.approx((OE + 21.0, ON + 19.0))


def test_only_a_closed_keep_out_outline_is_a_keep_out_area(plan):
    plan.add_outline("house", box(10, 10, 20, 18))
    plan.add_outline("drive", box(0, 0, 4, 10), "driveway")
    fence = plan.add_outline("fence", box(0, 0, 40, 30), "fence", keep_out=True)
    fence.closed = False
    assert [name for name, _ in plan.keep_out_areas()] == ["house"]


def test_outlines_round_trip_and_stamp_the_new_format(tmp_path, plan):
    plan.add_outline("house", box(10, 10, 20, 18))
    plan.add_outline("drive", box(0, 0, 4, 10), "driveway")
    back = Plan.load(plan.save(tmp_path / "p"))
    assert [(ln.line_id, ln.kind, ln.keep_out, ln.closed) for ln in back.lines] == [
        ("house", "building", True, True), ("drive", "driveway", False, True)]
    assert json.loads((tmp_path / "p.yardplan").read_text())["version"] == 2


def test_a_version_1_plan_opens_with_nothing_kept_out(tmp_path, plan):
    plan.add_line("swale", box(0, 0, 5, 5)[:2])
    payload = plan.to_dict()
    payload["version"] = 1
    for ln in payload["lines"]:
        del ln["keep_out"]
    path = tmp_path / "old.yardplan"
    path.write_text(json.dumps(payload), encoding="utf-8")
    back = Plan.load(path)
    assert back.lines[0].keep_out is False
    assert back.to_dict()["version"] == 2, "saved again, it says what it holds"


# --- editing -------------------------------------------------------------------------

def test_a_drawn_outline_drops_the_repeated_closing_corner(plan):
    ring = box(10, 10, 20, 18) + box(10, 10, 20, 18)[:1]
    house = PE.add_outline(plan, ring)
    assert house.line_id == "building-1" and len(house.numbers) == 4
    assert PE.add_outline(plan, ring, "street").line_id == "street-1"
    assert PE.add_outline(plan, ring).line_id == "building-2"
    assert PE.add_outline(plan, box(0, 0, 1, 1)[:2]) is None, "two clicks are not an area"


def test_renaming_an_outline_carries_what_hangs_off_it(plan):
    house = PE.add_outline(plan, box(10, 10, 20, 18))
    p = plan.add_point(OE + 12, ON + 5)
    p.method, p.ref_line, p.station_m, p.offset_m = "station_offset", house.line_id, 2.0, -1.0
    assert PE.rename_outline(plan, "building-1", "  the   house ") == "the house"
    assert plan.line("the house") is house and p.ref_line == "the house"
    PE.add_outline(plan, box(0, 0, 4, 4), "shed")
    with pytest.raises(PE.PlanEditError, match="already"):
        PE.rename_outline(plan, "shed-1", "the house")
    with pytest.raises(PE.PlanEditError, match="needs a name"):
        PE.rename_outline(plan, "shed-1", "  ")


def test_changing_the_kind_changes_the_corners_it_owns(plan):
    """Corners still shot as the old kind's become the new kind's; a corner
    given a purpose of its own keeps it. An automatic name follows."""
    bed = PE.add_outline(plan, box(10, 10, 20, 18))
    plan.by_number(bed.numbers[0]).purpose = "utility"
    name = PE.set_outline_kind(plan, "building-1", "landscaping")
    assert name == "landscaping-1" and bed.kind == "landscaping" and bed.keep_out
    assert [plan.by_number(n).purpose for n in bed.numbers] == [
        "utility", "bed edge", "bed edge", "bed edge"]
    assert PE.set_outline_kind(plan, name, "driveway") == "driveway-1"
    assert bed.keep_out is False
    PE.rename_outline(plan, "driveway-1", "front drive")
    assert PE.set_outline_kind(plan, "front drive", "street") == "front drive"


def test_an_open_outline_has_no_inside_to_keep_out(plan):
    fence = PE.add_outline(plan, box(0, 0, 40, 30), "other")
    PE.set_outline_closed(plan, fence.line_id, False)
    assert fence.keep_out is False
    with pytest.raises(PE.PlanEditError, match="no inside"):
        PE.set_outline_keep_out(plan, fence.line_id, True)
    PE.set_outline_closed(plan, fence.line_id, True)
    PE.set_outline_keep_out(plan, fence.line_id, True)
    assert fence.keep_out


def test_deleting_an_outline_asks_then_takes_its_corners(plan):
    plan.add_point(OE, ON)
    house = PE.add_outline(plan, box(10, 10, 20, 18))
    plan.by_number(house.numbers[0]).rod_in = 52.0
    with pytest.raises(PE.NeedsConfirmation, match="1 of them carry a reading"):
        PE.delete_outline(plan, house.line_id)
    assert len(plan.points) == 5
    assert PE.delete_outline(plan, house.line_id, confirm=True) == [2, 3, 4, 5]
    assert [p.number for p in plan.points] == [1] and plan.lines == []


def test_a_corner_inserted_after_the_last_goes_back_toward_the_first(plan):
    house = PE.add_outline(plan, box(10, 10, 20, 18))
    new = PE.insert_vertex(plan, house.numbers[-1])
    assert house.numbers[-1] == new.number
    assert (new.planned_e, new.planned_n) == pytest.approx((OE + 10, ON + 14))
    assert new.purpose == "building corner"


def test_the_summary_measures_the_outline_from_its_corners(plan):
    house = PE.add_outline(plan, box(10, 10, 20, 18))
    text = PE.outline_summary(plan, house)
    assert text.startswith("4 corners, 0 located")
    assert f"{80 / M_PER_FT ** 2:,.0f} sq ft" in text
    assert f"perimeter {36 / M_PER_FT:,.1f} ft" in text
    assert "(0.00 ft apart, from clicks)" in text
    for number in house.numbers:
        p = plan.by_number(number)
        p.observed_e, p.observed_n, p.method = p.planned_e, p.planned_n, "rtk"
    p.observed_e += 0.1
    text = PE.outline_summary(plan, house)
    assert "4 located" in text and "from clicks" not in text
    assert "0.00 ft apart" not in text, "a corner off square shows in the diagonals"


def test_a_terrain_shot_inside_a_keep_out_area_says_it_is_left_out(state):
    plan = state.plan
    spot = plan.add_point(OE + 15, ON + 14)
    loose = plan.add_point(OE + 5, ON + 5)
    PE.add_outline(plan, box(10, 10, 20, 18))
    payload = PE.plan_payload(plan, state.site)
    drawn = {p["number"]: p for p in payload["points"]}
    assert drawn[spot.number]["kept_out"]
    assert "inside keep-out outline building-1" in drawn[spot.number]["detail"]
    assert not drawn[loose.number]["kept_out"]
    corners = [p for p in payload["points"] if p["purpose"] == "building corner"]
    assert corners and not any(p["kept_out"] for p in corners)


def test_the_payload_lists_outlines_for_the_panel_and_the_map(state):
    PE.add_outline(state.plan, box(10, 10, 20, 18))
    payload = PE.plan_payload(state.plan, state.site)
    [outline] = payload["outlines"]
    assert outline["line_id"] == "building-1" and outline["keep_out"]
    assert outline["summary"].startswith("4 corners")
    [line] = payload["lines"]
    assert line["outline"] and line["keep_out"] and len(line["xy"]) == 5
    assert {"kind": "driveway", "keep_out": False} in payload["outline_kinds"]
    assert "building-1" in payload["line_ids"], "corners can be assigned in the table"


# --- tie transects -------------------------------------------------------------------

def test_tie_transects_keep_off_keep_out_ground():
    """A run may jump a 2 m gap in the cover - but not across a bed."""
    xs, ys = np.meshgrid(np.arange(0.25, 30, 0.5), np.arange(0.25, 20, 0.5))
    e, n = OE + xs.ravel(), ON + ys.ravel()
    bed = np.array(box(14, -1, 15.5, 21))                   # 1.5 m wide, full height
    inside = inside_polygons([bed], e, n)
    runs = PE.tie_transect_runs(e[~inside], n[~inside])
    assert any(r["axis"] == "E-W" and r["length_m"] > 25 for r in runs), \
        "without the outline the runs jump the uncovered strip"
    runs = PE.tie_transect_runs(e, n, keep_out=[bed])
    for r in runs:
        (x0, y0), (x1, y1) = r["vertices"]
        crosses = min(x0, x1) < OE + 15.5 and max(x0, x1) > OE + 14
        assert not crosses, r


# --- the surface ------------------------------------------------------------------------

def test_nothing_inside_a_keep_out_area_is_surfaced(state):
    house = np.array(box(10, 10, 20, 18))
    plain = build_surface(state.result, state.site, size=160)
    kept = build_surface(state.result, state.site, size=160, keep_out=[house])
    gx, gy = kept.grid_axes()
    inside = inside_polygons([house], *np.meshgrid(gx, gy))
    assert inside.sum() > 100
    assert kept.mask[inside].all(), "the rim inside the wall is masked too"
    assert not plain.mask[inside].all(), "the distance mask alone leaves it"
    assert kept.n_cells < plain.n_cells
    assert np.array_equal(kept.mask[~inside], plain.mask[~inside])


def test_an_outline_over_every_sample_is_refused_not_gridded(state):
    with pytest.raises(ValueError, match="keep-out"):
        build_surface(state.result, state.site, size=64,
                      keep_out=[box(-5, -5, 45, 35)])


def test_a_keep_out_outline_takes_its_points_out_of_the_result(state):
    before = len(state.result)
    PE.add_outline(state.plan, box(10, 10, 20, 18))
    state.plan_changed()
    assert state.kept_out > 0
    assert len(state.result) == before - state.kept_out
    d = state.result.df
    assert not inside_polygons(state.keep_out(), d["e"], d["n"]).any()
    assert state.surface.mask[inside_polygons(
        state.keep_out(), *np.meshgrid(*state.surface.grid_axes()))].all()
    from gpsrtk.app import views
    assert "inside keep-out outlines left out" in views.points_note(state)

    # Ticking keep-out off puts them back.
    PE.set_outline_keep_out(state.plan, "building-1", False)
    state.plan_changed()
    assert state.kept_out == 0 and len(state.result) == before


def test_only_an_edit_that_moves_a_keep_out_area_rebuilds_the_surface(state):
    PE.add_outline(state.plan, box(10, 10, 20, 18))
    state.plan_changed()
    surface = state.surface
    state.plan.add_point(OE + 2, ON + 2)
    state.plan_changed()
    assert state.surface is surface
    corner = state.plan.by_number(state.plan.line("building-1").numbers[0])
    PE.move_point(state.plan, corner.number, corner.planned_e - 1, corner.planned_n)
    state.plan_changed()
    assert state.surface is not surface


def test_laser_shots_inside_a_keep_out_area_are_left_out(state):
    """Spot 7 of the synthetic lot stands at (25, 15)."""
    state.solve_vertical("local")
    assert len(state.laser_points()) == 12
    PE.add_outline(state.plan, box(22, 12, 28, 18), "landscaping")
    state.plan_changed()
    laser = state.laser_points()
    assert len(laser) == 11
    assert not inside_polygons(state.keep_out(), laser["e"], laser["n"]).any()


def test_a_project_keeps_its_outlines(state, tmp_path):
    from gpsrtk.app import AppState
    from gpsrtk.site import example_site

    PE.add_outline(state.plan, box(10, 10, 20, 18))
    PE.rename_outline(state.plan, "building-1", "house")
    state.plan_changed()
    kept = state.kept_out
    path = state.save_project(tmp_path / "p.yardproj")

    again = AppState(site=example_site(), cache_dir=tmp_path / "cache")
    again.load_project(path)
    assert [ln.line_id for ln in again.plan.lines] == ["house"]
    assert again.kept_out == kept


# --- the field sheet ----------------------------------------------------------------

def test_the_field_sheet_draws_outlines_and_lists_their_corners(tmp_path, plan):
    from gpsrtk.io.fieldsheet import write_field_sheet

    house = plan.add_outline("house", box(10, 10, 20, 18))
    plan.add_outline("lot", box(0, 0, 40, 30), "property line")
    doc = write_field_sheet(plan, tmp_path / "sheet.html").read_text(encoding="utf-8")
    assert 'fill="url(#keep-out)"' in doc and 'id="keep-out"' in doc
    assert ">house (keep-out)</text>" in doc and ">lot</text>" in doc
    for number in house.numbers:
        assert f">{number}</td>" in doc
    assert doc.count("<td>house</td>") == 4, "each corner's row names its outline"
    assert "https://" not in doc


# --- through the API -----------------------------------------------------------------

@pytest.fixture
def client(state):
    from gpsrtk.web import create_app

    return TestClient(create_app(state, vector_providers={}),
                      base_url="http://127.0.0.1", headers={"X-Yard-Survey": "1"})


def post(client, url, body=None, status=200):
    r = client.post(url, json=body or {})
    assert r.status_code == status, r.text
    return r.json()


def test_an_outline_is_drawn_edited_and_deleted_through_the_api(client, state):
    ring = [[10.0, 10.0], [20.0, 10.0], [20.0, 18.0], [10.0, 18.0], [10.0, 10.0]]
    reply = post(client, "/api/plan/outline/add", {"vertices": ring, "kind": "shed"})
    assert reply["outline"] == "shed-1"
    assert reply["state"]["plan"]["outlines"][0]["keep_out"]
    assert "inside keep-out outlines" in reply["state"]["points_note"]

    reply = post(client, "/api/plan/outline/edit", {"line_id": "shed-1", "name": "garage"})
    assert reply["outline"] == "garage"
    reply = post(client, "/api/plan/outline/edit", {"line_id": "garage", "keep_out": False})
    assert reply["state"]["plan"]["outlines"][0]["keep_out"] is False
    assert state.kept_out == 0

    error = post(client, "/api/plan/outline/edit",
                 {"line_id": "garage", "kind": "gazebo"}, 400)["error"]
    assert error["level"] == "warning" and "not a kind" in error["text"]

    reply = post(client, "/api/plan/outline/delete", {"line_id": "garage"})
    assert reply["confirm"]["title"] == "Delete outline"
    assert len(state.plan.points) == 4
    post(client, "/api/plan/outline/delete", {"line_id": "garage", "confirm": True})
    assert state.plan.points == [] and state.plan.lines == []
