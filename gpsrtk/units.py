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

import re

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


# --- rod readings as written -----------------------------------------------

_NUM = r"(?:\d+(?:\.\d*)?|\.\d+)"
_FRAC = r"\d+\s*/\s*\d+"
# Inches, optionally with a fraction: 63, 63.25, 63 1/4, 63-1/4, 1/4.
_INCHES = re.compile(
    rf"^(?:(?P<whole>{_NUM})(?:\s*-\s*|\s+)(?P<frac>{_FRAC})"
    rf"|(?P<plain>{_NUM})|(?P<only>{_FRAC}))$")
# A grade rod read as feet-inches-fraction: 5-3-1/4, or feet-inches: 5-3.
_GRADE = re.compile(
    rf"^(?P<ft>\d+)\s*-\s*(?P<inch>{_NUM})(?:\s*-\s*(?P<frac>{_FRAC}))?$")
_VULGAR = {"¼": " 1/4", "½": " 1/2", "¾": " 3/4", "⅛": " 1/8", "⅜": " 3/8",
           "⅝": " 5/8", "⅞": " 7/8", "⅙": " 1/6", "⅓": " 1/3", "⅔": " 2/3"}


def _fraction(text: str) -> float:
    num, den = (int(part) for part in text.replace(" ", "").split("/"))
    if den == 0:
        raise ValueError(f"'{text}' divides by zero")
    return num / den


def _inches(text: str, whole_text: str) -> float:
    m = _INCHES.match(text)
    if not m:
        raise ValueError(f"'{whole_text}' is not a rod reading")
    if m["plain"] is not None:
        return float(m["plain"])
    if m["only"] is not None:
        return _fraction(m["only"])
    if "." in m["whole"]:
        raise ValueError(f"'{whole_text}' has both a decimal and a fraction")
    frac = _fraction(m["frac"])
    if frac >= 1.0:
        raise ValueError(f"'{whole_text}': the fraction should be under 1")
    return float(m["whole"]) + frac


def parse_rod(text) -> float:
    """A rod reading as written in the field, in inches.

    The rod is read in feet, inches and fractions, and SW Maps' number field
    has pushed readings into inches with fractions. All of these are accepted:

        63   63.25   63 1/4   63-1/4             inches
        5' 3 1/4"   5'3.25"   5' 3-1/4   5 ft 3 1/4 in
        5-3-1/4   5-3                            feet-inches(-fraction), as
                                                 read off a grade rod

    A bare number is inches, as it always has been. Negatives and anything
    unreadable raise ValueError with a message meant for the user.
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        value = float(text)
        if value != value:
            raise ValueError("no rod reading")
        if value < 0:
            raise ValueError("a rod reading cannot be negative")
        return value

    raw = str(text).strip()
    s = raw.lower()
    for glyph, spelled in _VULGAR.items():
        s = s.replace(glyph, spelled)
    for mark in ("′", "’", "‘", "`"):
        s = s.replace(mark, "'")
    for mark in ("″", "”", "“", "''"):
        s = s.replace(mark, '"')
    s = re.sub(r"\s*(?<![a-z])(?:feet|foot|ft)(?![a-z])\.?", "'", s)
    s = re.sub(r"\s*(?<![a-z])(?:inches|inch|in)(?![a-z])\.?", '"', s)
    s = s.strip()
    if not s:
        raise ValueError("no rod reading")
    if s.startswith("-"):
        raise ValueError("a rod reading cannot be negative")

    if "'" in s:
        feet_text, _, rest = s.partition("'")
        feet_text = feet_text.strip()
        if not re.fullmatch(_NUM, feet_text):
            raise ValueError(f"'{raw}' is not a rod reading")
        rest = rest.strip()
        if rest.endswith('"'):
            rest = rest[:-1].strip()
        rest = rest.lstrip("-").strip()
        if "'" in rest or '"' in rest:
            raise ValueError(f"'{raw}' is not a rod reading")
        inches = _inches(rest, raw) if rest else 0.0
        if inches >= 12.0:
            raise ValueError(f"'{raw}': the inches should be under 12")
        return float(feet_text) * IN_PER_FT + inches

    if s.endswith('"'):
        s = s[:-1].strip()
    grade = _GRADE.match(s)
    if grade and "/" not in grade["inch"]:
        inches = float(grade["inch"])
        if grade["frac"] is not None:
            if "." in grade["inch"]:
                raise ValueError(f"'{raw}' has both a decimal and a fraction")
            frac = _fraction(grade["frac"])
            if frac >= 1.0:
                raise ValueError(f"'{raw}': the fraction should be under 1")
            inches += frac
        if inches >= 12.0:
            raise ValueError(f"'{raw}': the inches should be under 12")
        return float(grade["ft"]) * IN_PER_FT + inches
    return _inches(s, raw)
