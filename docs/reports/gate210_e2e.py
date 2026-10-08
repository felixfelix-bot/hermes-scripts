#!/usr/bin/env python3
"""Independent end-to-end verification of Gate 2.10 enforcement (NOT the worker's suite).

Builds a scratch kanban board, inserts cards, and runs the REAL
gate_engine.evaluate_task() over them. Proves:

  A  an untagged code card is unaffected            (no fleet-wide blast radius)
  B  a pcb-tagged card with no evidence is BLOCKED  (both predicates missing)
  C  a pcb-tagged card with valid review+consult    -> both PASS
  D  a tier/* alias as reviewer_model is REJECTED
  E  an artifact that does not exist / is tiny      -> REJECTED
  F  a valid review with no consult                 -> pcb_consult missing
  G  a consult from the reviewer's own family       -> pcb_consult REJECTED

Run: python3 gate210_e2e.py
"""
import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

GE = Path("~/.hermes/scripts/gate_engine.py")
spec = importlib.util.spec_from_file_location("ge_under_test", GE)
ge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ge
spec.loader.exec_module(ge)

ROOT = Path(tempfile.mkdtemp(prefix="gate210-"))
BOARD = "scratch"
(ROOT / BOARD).mkdir(parents=True)
DB = ROOT / BOARD / "kanban.db"

SCHEMA = """
create table tasks (
  id text primary key, title text, assignee text, status text, result text,
  body text, completed_at integer, created_at integer, priority integer default 0,
  urgency text);
create table task_comments (task_id text, body text, created_at integer);
"""

ART = ROOT / "REVIEW-artifact.md"
ART.write_text("# cold cross-family PCB review\n" + ("x" * 400) + "\n")

REVIEW_OK = (f"pcb_review: verdict=APPROVED reviewer_model=kimi-k3 "
             f"reviewer_profile=pcb-reviewer artifact={ART} drc=0 erc=0 netlist_parity=ok")
CONSULT_OK = (f"pcb_consult: verdict=APPROVED reviewer_model=deepseek/deepseek-v4-pro "
              f"reviewer_profile=pcb-consultant artifact={ART}")


def card(cid, tags, result="", body="", assignee="worker-balloon"):
    """`tags` is the card's `tags:` declaration line — stored in the BODY, which
    is where the engine looks for the first tags declaration."""
    now = int(time.time())
    conn = sqlite3.connect(DB)
    conn.execute("insert into tasks (id,title,assignee,status,result,body,"
                 "completed_at,created_at) values (?,?,?,'done',?,?,?,?)",
                 (cid, f"card {cid}", assignee, result,
                  (tags + "\n" + body) if tags else body, now, now))
    conn.commit(); conn.close()


def run(cid):
    return ge.evaluate_task(BOARD, cid)


def gates_of(res, key):
    return [g for g in (res.get(key) or [])]


def main():
    ge.BOARDS = ROOT
    conn = sqlite3.connect(DB); conn.executescript(SCHEMA); conn.commit(); conn.close()

    card("t_plain", "", result="Refactored the parser. tests_green: 42/42")
    card("t_pcb_empty", "tags: pcb", result="Board routed and gerbers plotted.")
    card("t_pcb_ok", "tags: pcb",
         result=f"Board done.\n{REVIEW_OK}\n{CONSULT_OK}\n")
    card("t_pcb_alias", "tags: schematic",
         result="done\npcb_review: verdict=APPROVED reviewer_model=tier/schematic-review "
                "reviewer_profile=pcb-reviewer artifact=%s drc=0 erc=0 netlist_parity=ok\n%s"
                % (ART, CONSULT_OK))
    card("t_pcb_noart", "tags: board",
         result="done\npcb_review: verdict=APPROVED reviewer_model=kimi-k3 "
                "reviewer_profile=pcb-reviewer artifact=/nope/missing.md drc=0 erc=0 "
                "netlist_parity=ok\n" + CONSULT_OK)
    card("t_pcb_noconsult", "tags: kicad", result=f"done\n{REVIEW_OK}\n")
    card("t_pcb_samefam", "tags: gerber",
         result="done\n" + REVIEW_OK + "\n" +
                "pcb_consult: verdict=APPROVED reviewer_model=kimi-k3 "
                f"reviewer_profile=pcb-consultant artifact={ART}")

    results, failures = {}, []
    for cid in ("t_plain", "t_pcb_empty", "t_pcb_ok", "t_pcb_alias",
                "t_pcb_noart", "t_pcb_noconsult", "t_pcb_samefam"):
        r = run(cid)
        results[cid] = {k: r.get(k) for k in
                        ("tier", "verdict", "missing", "pcb_tagged", "author_family")}
        results[cid]["pcb_review"] = (r.get("pcb_review") or {}).get("reason") if r.get("pcb_review") else None
        results[cid]["pcb_consult"] = (r.get("pcb_consult") or {}).get("reason") if r.get("pcb_consult") else None

    def expect(name, cond):
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    print("=== A. untagged code card ===")
    r = results["t_plain"]
    print(f"      tagged={r['pcb_tagged']} missing={r['missing']}")
    expect("untagged card does not require pcb_review", "pcb_review" not in gates_of(
        run("t_plain"), "missing"))
    expect("untagged card does not require pcb_consult", "pcb_consult" not in gates_of(
        run("t_plain"), "missing"))

    print("=== B. pcb-tagged, no evidence ===")
    r = results["t_pcb_empty"]
    print(f"      tagged={r['pcb_tagged']} verdict={r['verdict']} missing={r['missing']}")
    expect("tagged card IS tagged", r["pcb_tagged"])
    expect("pcb_review missing", "pcb_review" in r["missing"])
    expect("pcb_consult missing", "pcb_consult" in r["missing"])
    expect("verdict blocks", r["verdict"] == "block")

    print("=== C. pcb-tagged, valid review + consult ===")
    r = results["t_pcb_ok"]
    print(f"      verdict={r['verdict']} missing={r['missing']}")
    expect("pcb_review passes", "pcb_review" in gates_of(run("t_pcb_ok"), "passed"))
    expect("pcb_consult passes", "pcb_consult" in gates_of(run("t_pcb_ok"), "passed"))
    expect("neither in missing", not [g for g in r["missing"] if g.startswith("pcb_")])

    print("=== D. tier alias as reviewer_model ===")
    r = results["t_pcb_alias"]
    print(f"      reason={r['pcb_review']}")
    expect("alias rejected", "pcb_review" in r["missing"])

    print("=== E. artifact does not exist ===")
    r = results["t_pcb_noart"]
    print(f"      reason={r['pcb_review']}")
    expect("missing artifact rejected", "pcb_review" in r["missing"])

    print("=== F. valid review, no consult ===")
    r = results["t_pcb_noconsult"]
    print(f"      reason={r['pcb_consult']}")
    expect("consult missing", "pcb_consult" in r["missing"])
    expect("review still passes", "pcb_review" not in r["missing"])

    print("=== G. consult in the reviewer's family ===")
    r = results["t_pcb_samefam"]
    print(f"      reason={r['pcb_consult']}")
    expect("same-family consult rejected", "pcb_consult" in r["missing"])

    print(f"\n=== raw verdicts ===")
    print(json.dumps(results, indent=2)[:1800])
    print(f"\n{'ALL GREEN' if not failures else 'FAILURES: ' + ', '.join(failures)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
