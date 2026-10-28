"""Supported US equity fundamental fields of the QC adapter. Generated; do not edit.

Made by tools/write_fundamental_catalogue.py from LEAN 2.5.0.0.18207's Fundamental
classes (tools/fundamental_catalogue_qc.py, run on a QuantConnect research kernel). A
name is ``morningstar.`` and the Python attribute path; a multi-period field's value
is read as ``name.period``. Types: float, int, bool, str, date. Units are as
Morningstar reports them; the catalogue does not state them.
"""

from . import part_1, part_2

LEAN_VERSION = "2.5.0.0.18207"
VERSION = "lean-2.5.0.0.18207"

# Period sets, referred to by index in FIELDS.
PERIODS = (
    ('one_year', 'three_years', 'three_months', 'five_years'),
    ('one_year', 'three_years', 'five_years'),
    ('one_year', 'one_month', 'two_months', 'three_months', 'six_months', 'nine_months'),
    ('one_year', 'three_months', 'six_months'),
    ('one_year',),
    ('five_years',),
    ('one_year', 'three_months'),
    ('three_months', 'one_year'),
    ('three_months', 'six_months', 'nine_months', 'one_year'),
    ('three_months', 'six_months', 'nine_months', 'one_year', 'three_years', 'five_years'),
    ('six_months', 'one_year'),
    ('one_year', 'three_months', 'three_years', 'five_years'),
    ('one_year', 'two_years', 'three_years', 'five_years', 'ten_years'),
    ('one_year', 'two_years', 'three_years'),
    ('one_month', 'two_months', 'three_months', 'six_months', 'nine_months', 'twelve_months'),
    ('three_months', 'six_months', 'nine_months', 'twelve_months'),
    ('one_month', 'three_months', 'six_months', 'nine_months', 'twelve_months'),
    ('two_months', 'three_months', 'six_months', 'nine_months', 'twelve_months'),
    ('twelve_months',),
    ('three_months', 'nine_months', 'twelve_months'),
    ('three_months', 'six_months', 'twelve_months'),
    ('six_months', 'twelve_months'),
    ('one_month', 'three_months', 'twelve_months'),
    ('three_months', 'twelve_months'),
    ('one_month', 'three_months', 'six_months', 'twelve_months'),
    ('one_month', 'two_months', 'three_months', 'nine_months', 'twelve_months'),
    ('one_month', 'three_months', 'nine_months', 'twelve_months'),
    ('two_months', 'three_months', 'six_months', 'twelve_months'),
    ('two_months', 'three_months', 'twelve_months'),
)

# name: (type, index into PERIODS, or None for a single value), from the parts.
FIELDS = {}
FIELDS.update(part_1.FIELDS)
FIELDS.update(part_2.FIELDS)


def resolve(name):
    """(attribute path, type) for a readable name, or None when it is not supported."""
    entry = FIELDS.get(name)
    if entry is not None:
        # A multi-period field must be read with its period named.
        return (tuple(name.split(".")[1:]), entry[0]) if entry[1] is None else None
    base, _, period = name.rpartition(".")
    entry = FIELDS.get(base)
    if entry is None or entry[1] is None or period not in PERIODS[entry[1]]:
        return None
    return (tuple(name.split(".")[1:]), entry[0])
