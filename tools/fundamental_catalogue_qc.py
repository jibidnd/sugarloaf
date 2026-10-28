"""List LEAN's US equity fundamental fields by .NET reflection (run on a QC research kernel).

The adapter's catalogue of supported fundamental fields is generated from the LEAN
version QuantConnect runs, not from local stubs. This script walks the public properties
of ``QuantConnect.Data.Fundamental.Fundamental`` under its Morningstar groups, records each
leaf's Python attribute path and type, and lists the periods of every multi-period field.
It prints one JSON summary between SUMMARY_JSON_BEGIN and SUMMARY_JSON_END;
``tools/write_fundamental_catalogue.py`` turns it into
``sugarloaf/adapters/quantconnect/fundamental_catalogue.py``.

Run it with any QC research runner that captures that summary, for example Sugarloaf
Lab's ``tools/qc_research.py``.
"""

import json

import clr
import QuantConnect.Data.Fundamental as F
from QuantConnect import Globals

# The Morningstar groups of a Fundamental object. Price, volume, dollar volume and market
# cap stay typed fields on the neutral record; they are not repeated here.
GROUPS = ("CompanyReference", "SecurityReference", "AssetClassification", "CompanyProfile",
          "ValuationRatios", "OperationRatios", "EarningRatios", "EarningReports",
          "FinancialStatements")
SCALARS = {"System.Double": "float", "System.Decimal": "float", "System.Single": "float",
           "System.Int32": "int", "System.Int64": "int", "System.Int16": "int",
           "System.Boolean": "bool", "System.String": "str", "System.DateTime": "date"}
SKIP = {"Item", "Symbol", "Time", "EndTime", "DataType", "IsFillForward", "Price",
        "Value", "EndTime"}


def underlying(t):
    """The value type of Nullable<T>, else ``t`` itself."""
    if t.IsGenericType and t.GetGenericTypeDefinition().FullName == "System.Nullable`1":
        return t.GetGenericArguments()[0]
    return t


def is_multi_period(t):
    base = t
    while base is not None:
        if base.Name.startswith("MultiPeriodField"):
            return True
        base = base.BaseType
    return False


def python_name(owner, name):
    """The snake_case attribute pythonnet exposes for a .NET property, by matching dir()."""
    py = getattr(F, owner.Name, None)
    key = name.lower()
    for attr in dir(py) if py is not None else ():
        if attr.replace("_", "").lower() == key and attr != name:
            return attr
    return name


def snake(name):
    out = ""
    for i, ch in enumerate(name):
        if ch.isupper() and i and (not name[i - 1].isupper() or
                                   (i + 1 < len(name) and name[i + 1].islower())):
            out += "_"
        out += ch.lower()
    return out


fields, problems = {}, []


def walk(t, path, names, depth):
    if depth > 6:
        return
    for prop in t.GetProperties():
        if prop.Name in SKIP or prop.GetIndexParameters().Length:
            continue
        ptype = underlying(prop.PropertyType)
        attr = python_name(t, prop.Name)
        here_path = path + [attr]
        here_names = names + [snake(prop.Name)]
        full = str(ptype.FullName)
        if is_multi_period(ptype):
            periods = []
            for p in ptype.GetProperties():
                pt = underlying(p.PropertyType)
                if p.DeclaringType.Name == ptype.Name and str(pt.FullName) in SCALARS:
                    periods.append([snake(p.Name), python_name(ptype, p.Name)])
            kind = "float"
            fields[".".join(["morningstar"] + here_names)] = {
                "type": kind, "path": here_path, "periods": periods}
        elif full in SCALARS:
            fields[".".join(["morningstar"] + here_names)] = {
                "type": SCALARS[full], "path": here_path, "periods": None}
        elif ptype.IsEnum:
            fields[".".join(["morningstar"] + here_names)] = {
                "type": "str", "path": here_path, "periods": None}
        elif full.startswith("QuantConnect.Data.Fundamental."):
            walk(ptype, here_path, here_names, depth + 1)
        else:
            problems.append([".".join(here_names), full])


root = clr.GetClrType(F.Fundamental)
for group in GROUPS:
    prop = root.GetProperty(group)
    if prop is None:
        problems.append([group, "missing group"])
        continue
    walk(underlying(prop.PropertyType), [python_name(root, group)], [snake(group)], 1)

summary = {"lean_version": str(Globals.Version), "fields": fields,
           "unsupported": problems, "count": len(fields)}
text = json.dumps(summary)
print("leaves", len(fields), "unsupported", len(problems))
print("SUMMARY_JSON_BEGIN")
for i in range(0, len(text), 150_000):
    print(text[i:i + 150_000], end="")
print("\nSUMMARY_JSON_END")
