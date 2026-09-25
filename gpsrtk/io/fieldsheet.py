"""Printable field sheet.

One page you can put on a clipboard: the planned shots drawn over the aerial so
you can find them on the ground, and a table of blank rows to write readings
into. The number is the only thing linking the two, and the only thing needed
to type the readings back in afterwards.

The map is the part that matters. A numbered list without one is a list of
places you cannot find; on 22 cm leaf-off imagery you can see the driveway
edge, the shrub beds and the ground under the trees, which is enough to stand
in the right spot.

Output is plain HTML with the basemap inlined as a data URI, so the file is
self-contained, works with no network in the field, and prints from any
browser. No PDF dependency for something a browser already does.
"""

from __future__ import annotations

import base64
import html
import io
from datetime import date
from pathlib import Path

import numpy as np

from ..plan import purpose_group
from ..units import m_to_ft

MAP_PX = 900          # rendered map size; keeps the inlined image reasonable

# Styled by purpose GROUP, not by individual purpose: what matters on the
# ground is whether a mark is terrain, a built feature, or control, and there
# are far too many purposes to give each its own symbol legibly.
GROUP_STYLE = {
    "terrain": ("#c0392b", "circle"),
    "feature": ("#1f5fa8", "square"),
    "control": ("#2b7a3d", "diamond"),
}


def _image_data_uri(layer, size: int = MAP_PX) -> tuple[str, tuple]:
    """Base64 PNG of the basemap, plus its extent."""
    from PIL import Image

    img = Image.fromarray(layer.image)
    if max(img.size) > size:
        img = img.resize((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    e = layer.extent
    return uri, (e.xmin, e.ymin, e.xmax, e.ymax)


def _marker(x: float, y: float, shape: str, colour: str) -> str:
    if shape == "diamond":
        return (f'<polygon points="{x},{y-7} {x+7},{y} {x},{y+7} {x-7},{y}" '
                f'fill="{colour}" stroke="white" stroke-width="1.6"/>')
    if shape == "square":
        return (f'<rect x="{x-6}" y="{y-6}" width="12" height="12" '
                f'fill="{colour}" stroke="white" stroke-width="1.6"/>')
    return (f'<circle cx="{x}" cy="{y}" r="6.5" fill="{colour}" '
            f'stroke="white" stroke-width="1.6"/>')


def _map_svg(plan, extent, uri: str | None, size: int = MAP_PX,
             offset=(0.0, 0.0)) -> str:
    xmin, ymin, xmax, ymax = extent
    width = max(xmax - xmin, 1e-9)
    height = max(ymax - ymin, 1e-9)
    off_e, off_n = offset

    def to_px(e, n):
        """World coordinates to pixels within the basemap image.

        The image fills the viewport at its own georeferenced extent, so an
        alignment shift cannot move the image - it moves everything drawn on
        top, the other way. Subtracting the offset here is what makes the
        printed sheet agree with what is on screen.
        """
        return ((e - off_e - xmin) / width * size,
                (1 - (n - off_n - ymin) / height) * size)

    def point_px(p):
        """A point is drawn where it is best known to be, as on screen."""
        e, n, _, _ = plan.resolve(p.number)
        return to_px(e, n)

    parts = [f'<svg viewBox="0 0 {size} {size}" class="plan" '
             f'xmlns="http://www.w3.org/2000/svg">']
    if uri:
        parts.append(f'<image href="{uri}" x="0" y="0" width="{size}" '
                     f'height="{size}"/>')
    else:
        parts.append(f'<rect width="{size}" height="{size}" fill="#f2f2f2"/>')

    for line in plan.lines:
        pts = []
        for num in line.numbers:
            p = plan.by_number(num)
            if p is not None:
                pts.append("%.1f,%.1f" % point_px(p))
        if len(pts) >= 2:
            closing = " " + pts[0] if line.closed else ""
            geometry = f'points="{" ".join(pts)}{closing}" fill="none"'
            # White casing underneath: a dashed blue line alone disappears
            # against tree canopy and shadowed grass, which is exactly where
            # these lines get drawn.
            parts.append(f'<polyline {geometry} stroke="#ffffff" '
                         f'stroke-width="5.5" opacity="0.85"/>')
            parts.append(f'<polyline {geometry} stroke="#1f5fa8" '
                         f'stroke-width="2.4" stroke-dasharray="9 5"/>')

    for setup in plan.setups:
        if setup.e is None or setup.n is None:
            continue
        x, y = to_px(setup.e, setup.n)
        parts.append(
            f'<path d="M{x},{y-11} L{x+10},{y+7} L{x-10},{y+7} Z" '
            f'fill="#e67e22" stroke="white" stroke-width="1.8"/>')
        parts.append(
            f'<text x="{x}" y="{y+22}" class="setup-label">{html.escape(setup.name)}</text>')

    for p in plan.points:
        colour, shape = GROUP_STYLE.get(purpose_group(p.purpose),
                                        GROUP_STYLE["terrain"])
        x, y = point_px(p)
        parts.append(_marker(x, y, shape, colour))
        parts.append(f'<text x="{x}" y="{y - 10}" class="num">{p.number}</text>')

    parts.append("</svg>")
    return "\n".join(parts)


def _rows(plan, site) -> str:
    """One row per shot, with somewhere to write every kind of answer.

    The locating columns are the point of the sheet as much as the rod reading
    is. In the field a position comes back as one of three things - a fixed
    GNSS shot, two tape distances to numbered points, or nothing at all because
    the planned mark was good enough - and each needs its own box. Leaving them
    to the notes column means transcribing free text later and guessing what
    was meant.
    """
    out = []
    for p in sorted(plan.points, key=lambda q: q.number):
        # Where to actually walk: the measured position once there is one,
        # otherwise the planned mark.
        e, n, _, _ = plan.resolve(p.number)
        if site is not None:
            le, ln = site.to_local(e, n)
            where = f"{m_to_ft(le):.0f}, {m_to_ft(ln):.0f}"
        else:
            where = f"{e:.1f}, {n:.1f}"
        line = next((ln.line_id for ln in plan.lines if p.number in ln.numbers), "")
        out.append(
            "<tr>"
            f'<td class="num-cell">{p.number}</td>'
            f"<td>{html.escape(p.purpose)}</td>"
            f"<td>{html.escape(line)}</td>"
            f"<td>{html.escape(p.setup)}</td>"
            f'<td class="coord">{where}</td>'
            '<td class="blank"></td>'        # rod reading
            '<td class="blank tick"></td>'   # GNSS fixed?
            '<td class="blank"></td>'        # GNSS point name or coordinates
            '<td class="blank tie"></td>'    # tie A: # / ft
            '<td class="blank tie"></td>'    # tie B: # / ft
            '<td class="blank wide"></td>'   # notes
            "</tr>")
    return "\n".join(out)


def _setup_blocks(plan) -> str:
    """A closure block per planned laser setup, or two blank ones.

    The network has few degrees of freedom, so a mis-read rod hides easily.
    Reading the benchmark at the open AND the close of every setup is what
    catches one, and a periodic two-peg check is what catches a laser out of
    adjustment. Printing the boxes is what gets them done.
    """
    names = [html.escape(s.name) for s in plan.setups] or ["", ""]
    blocks = []
    for name in names:
        blocks.append(
            '<table class="setup">'
            f'<tr><th colspan="2">Laser setup {name or "____"}</th></tr>'
            '<tr><td>Backsight on ____ (station) at open</td>'
            '<td class="blank"></td></tr>'
            '<tr><td>Backsight on ____ at close</td>'
            '<td class="blank"></td></tr>'
            '<tr><td>Calibration (two-peg) check: date / result</td>'
            '<td class="blank"></td></tr>'
            '<tr><td>Observer</td><td class="blank"></td></tr>'
            '</table>')
    return '<div class="setups">' + "\n".join(blocks) + "</div>"


def write_field_sheet(plan, path: str | Path, *, basemap=None, site=None,
                      title: str = "", note: str = "",
                      imagery_offset=(0.0, 0.0)) -> Path:
    """Write a self-contained printable sheet for a shot plan."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    uri = None
    if basemap is not None:
        uri, extent = _image_data_uri(basemap.layer if hasattr(basemap, "layer")
                                      else basemap)
    else:
        es = [p.planned_e for p in plan.points] or [0.0]
        ns = [p.planned_n for p in plan.points] or [0.0]
        pad = 8.0
        half = max(max(es) - min(es), max(ns) - min(ns)) / 2 + pad
        cx, cy = (min(es) + max(es)) / 2, (min(ns) + max(ns)) / 2
        extent = (cx - half, cy - half, cx + half, cy + half)

    cov = plan.coverage()
    heading = html.escape(title or plan.name or "Shot plan")
    setups = ", ".join(html.escape(s.name) for s in plan.setups) or "none planned"

    doc = f"""<!doctype html>
<meta charset="utf-8">
<title>{heading} — field sheet</title>
<style>
  body {{ font: 13px/1.45 system-ui, "Segoe UI", sans-serif; color: #1a1a1a;
         margin: 18px; }}
  h1 {{ font-size: 19px; margin: 0 0 2px; }}
  .meta {{ color: #666; font-size: 12px; margin-bottom: 12px; }}
  .plan {{ width: 100%; max-width: 720px; border: 1px solid #bbb; }}
  .num {{ font: 700 13px sans-serif; fill: #fff; text-anchor: middle;
          paint-order: stroke; stroke: #000; stroke-width: 3px;
          stroke-linejoin: round; }}
  .setup-label {{ font: 700 11px sans-serif; fill: #e67e22; text-anchor: middle;
                  paint-order: stroke; stroke: #fff; stroke-width: 3px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 14px;
           font-size: 12px; }}
  th, td {{ border: 1px solid #999; padding: 4px 6px; text-align: left; }}
  th {{ background: #eee; font-size: 11px; text-transform: uppercase;
        letter-spacing: .03em; }}
  .num-cell {{ font-weight: 700; width: 34px; text-align: center; }}
  .coord {{ color: #666; white-space: nowrap; }}
  .blank {{ min-width: 74px; height: 30px; background: #fff; }}
  .blank.wide {{ min-width: 150px; }}
  .blank.tick {{ min-width: 34px; }}
  /* A faint divider so "#" and "ft" land in their own halves of the box
     instead of running together into one illegible number. */
  .blank.tie {{ min-width: 92px; background:
                linear-gradient(90deg, #fff 0 33%, #ccc 33% calc(33% + 1px),
                                #fff calc(33% + 1px) 100%); }}
  .setups {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }}
  table.setup {{ width: auto; margin-top: 0; }}
  table.setup td:first-child {{ white-space: nowrap; }}
  .legend {{ font-size: 12px; color: #444; margin-top: 10px; }}
  .legend b {{ color: #1a1a1a; }}
  .swatch {{ display: inline-block; width: 10px; height: 10px;
             vertical-align: -1px; margin-right: 3px; border: 1px solid #fff;
             outline: 1px solid #999; }}
  @page {{ size: landscape; margin: 8mm; }}
  @media print {{
    body {{ margin: 0; }}
    /* Landscape, because the locating columns are what make the sheet
       usable and they do not fit across a portrait page. */
    .plan {{ max-width: 165mm; page-break-after: avoid; }}
    tr {{ page-break-inside: avoid; }}
    thead {{ display: table-header-group; }}
  }}
</style>

<h1>{heading}</h1>
<div class="meta">
  {date.today():%d %b %Y} &middot; {cov['planned']} planned shots
  &middot; laser setups: {setups}
  {("&middot; " + html.escape(note)) if note else ""}
</div>

{_map_svg(plan, extent, uri, offset=imagery_offset)}

<div class="legend">
  <span class="swatch" style="background:#c0392b"></span>terrain
  <span class="swatch" style="background:#1f5fa8"></span>built feature
  <span class="swatch" style="background:#2b7a3d"></span>control / reference
  <span class="swatch" style="background:#e67e22"></span>laser setup
  &nbsp;&mdash;&nbsp; positions are <b>feet from the site origin</b>, east then north.
  <br>
  <b>Rod (in)</b> is the only column that must be filled in on every row.
  <br>
  <b>Position</b> &mdash; fill in whichever ONE applies:
  tick <b>Fixed</b> and note the receiver's point name or coordinates if the
  GNSS held a <b>fixed</b> solution;
  or write two <b>tape ties</b> as <i>point&nbsp;#</i> and <i>feet</i> to marks
  you did shoot;
  or leave both blank and the planned position stands, which on this lot costs
  under 2&nbsp;cm of height per foot of error.
  A <b>float</b> fix is worse than the planned position &mdash; leave it blank.
  <br>
  <b>Station names in SW Maps</b> &mdash; name each record after the point it
  was read on: a planned shot as <b>P</b> and its number (<b>P12</b>), a
  permanent mark by its name (<b>BM1</b>), a turning point by any name you
  like. The same name means the same physical point. Anything else can keep
  SW Maps' own ID.
</div>

{_setup_blocks(plan)}

<table>
  <thead>
    <tr>
      <th>#</th><th>Purpose</th><th>Line</th><th>Setup</th>
      <th>Plan E,N (ft)</th><th>Rod (in)</th><th>Fixed</th>
      <th>GNSS pt / E,N</th>
      <th>Tie A &nbsp;#&nbsp;|&nbsp;ft</th><th>Tie B &nbsp;#&nbsp;|&nbsp;ft</th>
      <th>Notes</th>
    </tr>
  </thead>
  <tbody>
{_rows(plan, site)}
  </tbody>
</table>
"""
    path.write_text(doc, encoding="utf-8")
    return path
