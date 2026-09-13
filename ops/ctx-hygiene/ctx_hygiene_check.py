import sqlite3, os
from datetime import datetime
base = os.path.expanduser('~/.hermes/kanban/boards')
now = int(datetime.now().timestamp())
cut = now - 7 * 86400
for b in ['admin', 'market', 'tollgate']:
    p = os.path.join(base, b, 'kanban.db')
    if not os.path.exists(p):
        print(b, 'MISSING'); continue
    con = sqlite3.connect(p); cur = con.cursor()
    cur.execute("select status,count(*) from tasks group by status")
    tot = cur.execute('select count(*) from tasks').fetchone()[0]
    print("=== %s === total=%d statuses=%s" % (b, tot, dict(cur.fetchall())))
    cur.execute("select id,title,status,created_at,completed_at from tasks where status not in ('done','completed','archived','cancelled') order by created_at")
    st = cur.fetchall()
    stale = [r for r in st if r[3] < cut]
    print("  open(non-terminal)=%d stale>7d=%d" % (len(st), len(stale)))
    print("  terminal=%d" % cur.execute("select count(*) from tasks where status in ('done','completed','archived')").fetchone()[0])
    for r in stale:
        age = (now - r[3]) / 86400
        print("    %-42s %-10s %6.1fd  %s" % (r[0], r[2], age, (r[1] or '')[:70]))
    con.close()
