"""Unit constants and conversions.

Single source of truth. These were previously redefined in three of the
archive scripts (`M_TO_FT = 3.280839895` in process_full.py and to_revit.py,
a bare `0.3048` in process_survey.py, `1.0 / 12.0` for rod readings).

Two foot definitions exist and they are not interchangeable:

  international foot  exactly 0.3048 m       - Revit, and all our exports
  US survey foot      exactly 1200/3937 m    - EPSG:3417 (Iowa North ftUS)
                                               and most US state plane systems

They differ by 2 ppm: about 1/8 inch per mile. Irrelevant across a
150 ft lot, but not across the ~4676 km of a raw UTM northing, so the
distinction matters the moment state plane data is reprojected. Always
name which foot is meant.
"""

# --- length ---------------------------------------------------------------

M_PER_FT = 0.3048                       # international foot, exact
FT_PER_M = 1.0 / M_PER_FT               # 3.2808398950131235

M_PER_FT_US = 1200.0 / 3937.0           # US survey foot, exact
FT_US_PER_M = 1.0 / M_PER_FT_US

FT_PER_IN = 1.0 / 12.0
IN_PER_FT = 12.0
M_PER_IN = M_PER_FT * FT_PER_IN


def m_to_ft(m):
    """Metres to international feet."""
    return m * FT_PER_M


def ft_to_m(ft):
    """International feet to metres."""
    return ft * M_PER_FT


def m_to_ft_us(m):
    """Metres to US survey feet. For state plane data only."""
    return m * FT_US_PER_M


def ft_us_to_m(ft):
    """US survey feet to metres. For state plane data only."""
    return ft * M_PER_FT_US


def in_to_ft(inches):
    """Rod readings are recorded in inches; elevations are carried in feet."""
    return inches * FT_PER_IN


def ft_to_in(ft):
    return ft * IN_PER_FT
