"""Names in the library, deployments, tools and docs say what things are.

Words that name a thing by what it imitates rather than what it is are refused in those
files: fake, stand-in, stub, mock, dummy, synthetic, fixture, placeholder, simulated,
"first version" (and their plurals).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("sugarloaf", "deployments", "tools", "docs", "examples")
SUFFIXES = {".py", ".md", ".toml", ".json", ".ipynb"}
BANNED = re.compile(
    r"\b(fakes?|stand-ins?|stubs?|mocks?|dumm(?:y|ies)|synthetic|fixtures?|placeholders?"
    r"|simulated|first versions?)\b",
    re.IGNORECASE,
)


def test_no_banned_words():
    found = []
    for folder in FOLDERS:
        for path in sorted((ROOT / folder).rglob("*")):
            if path.suffix not in SUFFIXES or not path.is_file():
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in BANNED.finditer(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {match.group(0)}")
    assert not found, "\n".join(found)
