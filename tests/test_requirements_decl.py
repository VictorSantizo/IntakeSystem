"""Every third-party module imported by root-level app code must be declared
in requirements.txt.

2026-07-27 code review, GH #5: Pillow — and, found on the deeper dive,
pypdfium2 — were imported directly but never declared. Both fail SOFT (lazy
imports with graceful degradation), so a fresh install didn't crash: it just
silently lost OCR, the vision fallback, and the page-image viewer, and nothing
else in the suite could notice. The first half (Pillow) got fixed and the
issue closed while the second half (pypdfium2) slipped through — this test
closes the loop generically instead of playing whack-a-mole per package.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# import name -> distribution name in requirements.txt, where they differ
_DIST = {"PIL": "Pillow", "fitz": "PyMuPDF"}


def _top_level_imports(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                yield node.module.split(".")[0]


def _declared() -> set:
    declared = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line:
            declared.add(re.split(r"[<>=!~\[; ]", line, maxsplit=1)[0].lower())
    return declared


def test_direct_imports_are_declared():
    local = {p.stem for p in ROOT.glob("*.py")}
    declared = _declared()
    missing = {}
    for py in sorted(ROOT.glob("*.py")):
        for mod in _top_level_imports(py):
            if mod in sys.stdlib_module_names or mod in local:
                continue
            dist = _DIST.get(mod, mod)
            if dist.lower() not in declared:
                missing.setdefault(f"{mod} (declare as {dist})", py.name)
    assert not missing, (
        "Imported by app code but not declared in requirements.txt "
        f"(fails soft on a fresh install, so nothing else will catch it): {missing}"
    )
