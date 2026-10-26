#!/usr/bin/env python3
"""Merge the Gate 2.10 pcb feature into master's gate_engine.py WITHOUT losing
master's newer delivery-evidence work (which the live file predates).

master = origin/master:gate_engine.py  (has delivery hardening: RE_DELIVERY_URL...)
live   = the deployed engine           (has the pcb feature, LACKS delivery work)

Result must be: master + pcb, with ZERO lines of master lost. Every edit is
anchored on a unique string; any ambiguity aborts.
"""
import json
import subprocess
import sys
from pathlib import Path

WT = Path("~/worktrees/gate210-fix")
LIVE_PATH = Path("~/.hermes/scripts/gate_engine.py")
master_raw = subprocess.run(["git", "-C", str(WT), "show", "origin/master:gate_engine.py"],
                            capture_output=True, text=True, check=True).stdout
live = LIVE_PATH.read_text()
assert master_raw, "no master content"


def once(hay, needle, what):
    n = hay.count(needle)
    if n != 1:
        sys.exit(f"ABORT: anchor {what!r} found {n} times (need exactly 1)")
    return hay.index(needle)


def block(hay, start, end, what):
    i, j = once(hay, start, what), once(hay, end, what + " (end)")
    if j <= i:
        sys.exit(f"ABORT: {what}: end marker precedes start")
    return hay[i:j]


# ---- extract the pcb pieces from the live file -------------------------------
blk_helpers = block(live, "# Gate 2.10 — pcb_review / pcb_consult circuit-board evidence gates.",
                    "def _names_other_ref(", "helpers block")
blk_eval = block(live, "    # Gate 2.10: circuit-board review/consult. Only applicable when the first",
                 '    enforce = spec.get("enforce"', "evaluate() hook")

i = once(live, "             pcb_tagged: bool = False,", "signature start")
sig_tail = live[i:live.index("\n", live.index("pcb_consult: dict | None = None", i)) + 1]

# master never reads the card body; the pcb trigger needs it, so carry the read too
blk_vars = block(live, "        body_row = conn.execute(",
                 "    finally:", "body read + pcb vars")

i = once(live, "    # Gate 2.10: circuit-board review/consult. Trigger from the first tags: line;",
         "evaluate_task hook start")
j = live.index("\n", live.index("pcb_consult = pcb_consult_present(", i)) + 1
blk_task = live[i:j]

i = once(live, "pcb_tagged=is_pcb_tagged, pcb_review=pcb_review,", "call args")
i = live.rfind("\n", 0, i) + 1                      # keep the line's indentation
j = live.index("\n", live.index("pcb_consult=pcb_consult)", i)) + 1
call_args = live[i:j]

# live's fragments carry the signature/call closers; master's anchor line supplies
# them, so strip them from the inserted fragments or the parens double up.
sig_tail = sig_tail.replace("pcb_consult: dict | None = None) -> dict:",
                            "pcb_consult: dict | None = None,")
call_args = call_args.replace("pcb_consult=pcb_consult)", "pcb_consult=pcb_consult,")

meta = []
i = live.index('"pcb_tagged": is_pcb_tagged,')
i = live.rfind("\n", 0, i) + 1
for key in ('"pcb_tagged": is_pcb_tagged,', '"pcb_review": pcb_review,', '"pcb_consult": pcb_consult,'):
    k = live.index(key, i)
    k = live.rfind("\n", 0, k) + 1
    meta.append(live[k:live.index("\n", k) + 1])
meta_block = "".join(meta)

print("extracted:")
print(f"  helpers   {len(blk_helpers.splitlines()):4d} lines")
print(f"  eval hook {len(blk_eval.splitlines()):4d} lines")
print(f"  sig tail  {len(sig_tail.splitlines()):4d} lines")
print(f"  task hook {len(blk_task.splitlines()):4d} lines")
print(f"  call args {len(call_args.splitlines()):4d} lines")
print(f"  meta      {len(meta_block.splitlines()):4d} lines")

# ---- splice into master ------------------------------------------------------
out = master_raw
def splice(text, anchor, insert, before=True, label=""):
    k = once(text, anchor, label or anchor[:40])
    k = text.rfind("\n", 0, k) + 1        # splice at the START of the anchor's line
    if before:
        return text[:k] + insert + text[k:]
    end = text.index("\n", k) + 1
    return text[:end] + insert + text[end:]

out = splice(out, "def _names_other_ref(", blk_helpers, label="insert helpers")
out = splice(out, "delivery_text: str | None = None) -> dict:", sig_tail, label="signature")
out = splice(out, '    enforce = spec.get("enforce"', blk_eval, label="evaluate hook")
out = splice(out, "        delivery = delivery_evidence_text(conn, task_id, result, completed_at)",
             blk_vars, label="body read + pcb vars")
out = splice(out, "    res = evaluate(tier, text, author, gates, ci_result=ci, ci_required=True,",
             blk_task, label="evaluate_task hook")
out = splice(out, "delivery_text=delivery)", call_args, label="call args")
out = splice(out, '"completed_at": completed_at, "created_at": created_at,',
             meta_block, label="res.update meta")

# ---- acceptance --------------------------------------------------------------
assert "<<<<<<<" not in out, "conflict markers in output"
WT.joinpath("gate_engine.py").write_text(out)
lost = [l for l in master_raw.splitlines() if l not in out.splitlines() and l.strip()]
print(f"\nacceptance:")
print(f"  lines of master lost: {len(lost)}")
for l in lost[:8]:
    print(f"    LOST: {l}")
for pat in ("RE_DELIVERY_URL", "RE_PLACEHOLDER_HOST", "pcb_review_present", "pcb_consult_present",
            "pcb_tagged", "pcb_evidence_text", "delivery_text"):
    print(f"  {pat:22s} {out.count(pat)}")
compile(out, "gate_engine.py", "exec")
print("  py_compile: OK")

# ---- spec: declare the gates in the repo copy --------------------------------
raw = subprocess.run(["git", "-C", str(WT), "show", "origin/master:gates.default.json"],
                     capture_output=True, text=True, check=True).stdout
d = json.loads(raw)
req = d["tiers"]["code"]["require"]
for g in ("pcb_review", "pcb_consult"):
    if g not in req:
        req.append(g)
WT.joinpath("gates.default.json").write_text(json.dumps(d, indent=2) + "\n")
print(f"  repo spec code.require now ends: {req[-4:]}")
