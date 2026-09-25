"""Rod readings as they are written in the field.

The rod is read in feet, inches and fractions, and SW Maps' number field has
pushed readings into inches with fractions. Every spelling the owner uses has
to arrive as the same number of inches, and a bare number stays inches.
"""

import math

import pytest

from gpsrtk.units import parse_rod


@pytest.mark.parametrize("text", [
    "63.25", "63 1/4", "63-1/4", "63 1/4\"", "63¼",
    "5' 3 1/4\"", "5'3.25\"", "5' 3-1/4", "5 ft 3 1/4 in", "5ft 3 1/4in",
    "5'-3 1/4\"", "5′ 3¼″",
    "5-3-1/4",
])
def test_every_spelling_of_the_same_reading_agrees(text):
    assert parse_rod(text) == pytest.approx(63.25)


@pytest.mark.parametrize("text, inches", [
    ("63", 63.0), (63, 63.0), (45.5, 45.5), ("5'", 60.0), ("5-3", 63.0),
    ("1/4", 0.25), ("5 feet 3 inches", 63.0), ("0", 0.0),
])
def test_plain_numbers_are_inches_and_feet_need_a_unit(text, inches):
    assert parse_rod(text) == pytest.approx(inches)


@pytest.mark.parametrize("text, why", [
    ("-3", "negative"), (-3.0, "negative"),
    ("forty", "not a rod reading"), ("x63", "not a rod reading"),
    ("6 3 1/4", "not a rod reading"), ("5'3'", "not a rod reading"),
    ("63 5/4", "under 1"), ("5' 13\"", "under 12"), ("5-13", "under 12"),
    ("63.5 1/4", "decimal and a fraction"), ("1/0", "zero"),
    ("", "no rod reading"), (math.nan, "no rod reading"),
])
def test_negatives_and_garbage_are_refused(text, why):
    with pytest.raises(ValueError, match=why):
        parse_rod(text)
