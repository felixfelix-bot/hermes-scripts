"""Remove debug/scratch artifacts from tests/ (kept out of the commit)."""
import glob
import os
import shutil

here = os.path.dirname(os.path.abspath(__file__))
patterns = [
    os.path.join(here, ".dbg*"),
    os.path.join(here, ".sdscratch.*"),
    os.path.join(here, ".t4.trace"),
    os.path.join(here, "t32probe.*"),
    os.path.join(here, "probe_cli_assumptions.py"),
]
removed = []
for pat in patterns:
    for p in glob.glob(pat):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
        removed.append(os.path.basename(p))
print("removed:", sorted(removed) or "(nothing)")
