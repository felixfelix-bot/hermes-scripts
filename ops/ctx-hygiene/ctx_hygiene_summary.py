import sqlite3, os, json
from datetime import datetime
base = os.path.expanduser('~/.hermes/kanban/boards')
now = int(datetime.now().timestamp())
cut = now - 7 * 86400
for b in ['admin', 'market', 'tollgate']:
    p = os.path.join(base, b, 'kanban.db')
    con = sqlite3.connect(p); cur = con.cursor()
    st = cur.execute("select id,title,status,created_at,assignee,block_kind from tasks "
                     "where status not in ('done','completed','archived','cancelled') order by created_at").fetchall()
    stale = [r for r in st if r[3] < cut]
    b30 = [r for r in stale if r[3] < now - 30 * 86400]
    b60 = [r for r in stale if r[3] < now - 60 * 86400]
    bystatus = {}
    byassign = {}
    for r in stale:
        bystatus[r[2]] = bystatus.get(r[2], 0) + 1
        byassign[r[4] or '(none)'] = byassign.get(r[4] or '(none)', 0) + 1
    open_total = len(st)
    tot = cur.execute('select count(*) from tasks').fetchone()[0]
    print("### %s: total=%d open=%d stale7=%d (>30d=%d, >60d=%d) pctstale=%.0f%%"
          % (b, tot, open_total, len(stale), len(b30), len(b60), 100.0 * len(stale) / max(open_total, 1)))
    print("   stale by status: %s" % bystatus)
    print("   stale by assignee: %s" % byassign)
    print("   newest stale: %s (%s, %.1fd)" % (stale[-1][0], stale[-1][1][:50], (now - stale[-1][3]) / 86400) if stale else "   none")
    con.close()
