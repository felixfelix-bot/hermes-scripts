#!/usr/bin/env python3
"""Host-driven-bench pipeline advancer: self-heal links, unblock ready children,
re-block premature promotions. Silent (empty stdout) unless it acts."""
import subprocess, sqlite3, sys, os

BOARD = "host-driven-bench"
DB = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/kanban.db")

def hermes(*a):
    return subprocess.run(["hermes","kanban","--board",BOARD,*a], capture_output=True, text=True)

db = sqlite3.connect(DB)
rows = db.execute("SELECT id,status FROM tasks").fetchall()
db.close()
status = {i:s for i,s in rows}
parents = {}
for i,_ in rows:
    out = hermes("show", i).stdout
    pl = [l for l in out.splitlines() if l.strip().startswith("parents:")]
    parents[i] = []
    if pl and "none" not in pl[0].lower():
        parents[i] = [p for p in pl[0].split("parents:")[1].split() if p.startswith("t_")]

acted = []
for i,s in status.items():
    ps = parents.get(i,[])
    done = all(status.get(p) in ("done","archived") for p in ps)
    if s == "blocked" and ps and done:
        hermes("unblock", i); acted.append(f"unblocked {i}")
    elif s in ("ready","running") and ps and not done:
        if s == "running":
            hermes("reclaim", i)
        hermes("block", i); acted.append(f"re-blocked {i} (parents not done)")
# final ping
if status.get("t_aef96f67") == "done":  # MRG1
    marker = "/tmp/hdb_pinged"
    if not os.path.exists(marker):
        open(marker,"w").write("1")
        acted.append("PIPELINE COMPLETE: MRG1 done — Stage A+B shipped")
if acted:
    print("HDB-ADVANCER: " + "; ".join(acted))
