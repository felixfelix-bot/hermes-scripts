#!/usr/bin/env python3
"""Host-driven-bench pipeline advancer: self-heal links, unblock ready children,
re-block premature promotions. Silent (empty stdout) unless it acts."""
import subprocess, sqlite3, sys, os

BOARD = "decode-gaps"
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
# final ping: all six done
ALL = ["t_7b922684","t_7d5efd56","t_2696cfef","t_7461f3e1","t_43acc435","t_e67cdcdb","t_1b57c68c","t_b49ae8f3","t_8c237820","t_3ba8772f","t_01b335bb","t_57bbeff4","t_5bcb3130"]
if all(status.get(x) in ("done","archived") for x in ALL):
    marker = "/tmp/dg_pinged"
    if not os.path.exists(marker):
        open(marker,"w").write("1")
        acted.append("DECODE-GAPS COMPLETE: N1-N6 all done")
