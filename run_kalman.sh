#!/usr/bin/env bash
# Kalman price-intelligence cycle — runs after each IndiaMART scrape.
#   pull snapshots (VPS2) -> ingest -> Kalman predict -> dashboard -> anomaly alert
# Designed as a cron watchdog: stdout is delivered verbatim each run.
# Exit 0 always (anomalies are reported, not errors); non-zero only on hard failure.
set -uo pipefail
cd /home/c03rad0r/price-insight

STAMP="$(date -u '+%Y-%m-%d %H:%M UTC')"
LOG="$(mktemp)"

python3 price_kalman.py --pull --db price_trends.db --json predictions.json >"$LOG" 2>&1
RC=$?
if [ $RC -ne 0 ]; then
  echo "🔴 [Kalman Price Intel] $STAMP — pipeline FAILED (rc=$RC)"
  echo "VPS2 pull or prediction error. Log:"
  tail -20 "$LOG"
  rm -f "$LOG"
  exit 1
fi
rm -f "$LOG"

python3 dashboard.py --db price_trends.db --out dashboard.html >/dev/null 2>&1

# Anomaly alert + signal summary (latest scrape vs predicted ±2σ band)
python3 - <<'PY'
import sqlite3, datetime
c = sqlite3.connect("/home/c03rad0r/price-insight/price_trends.db")
c.row_factory = sqlite3.Row
now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
n_scrapes = c.execute("SELECT COUNT(DISTINCT ts) FROM snapshots").fetchone()[0]
n_products = c.execute("SELECT COUNT(DISTINCT product_id) FROM snapshots").fetchone()[0]
print(f"📊 [Kalman Price Intel] {now} — {n_products} products, {n_scrapes} scrapes")
alerts = []
lines = []
for r in c.execute(
        "SELECT p.product_id, p.level, p.velocity, p.lo2, p.hi2, p.sigma, p.trend, p.signal, p.mape "
        "FROM predictions p WHERE p.metric='median' ORDER BY p.product_id"):
    # latest observed median for this product
    lo = c.execute("SELECT median, scrape_time FROM snapshots WHERE product_id=? AND median IS NOT NULL ORDER BY ts DESC LIMIT 1",
                   (r["product_id"],)).fetchone()
    last = lo["median"] if lo else None
    name = r["product_id"].replace("-"," ").title()
    z = "—"
    flag = False
    if last is not None and r["sigma"]:
        z = (last - r["level"]) / r["sigma"] if r["sigma"] else 0
        if abs(z) > 2.0:
            flag = True
    glyph = {"rising":"▲","falling":"▼","stable":"►"}.get(r["trend"],"•")
    line = f"{name:30s} ₹{r['level'] or 0:>8.0f} {glyph}{r['trend']:8s} {r['signal']:8s} vel={r['velocity']:+.1f} z={z if isinstance(z,str) else round(z,2)}"
    if flag:
        alerts.append(f"⚠️ {name}: latest ₹{last:.0f} is {z:+.1f}σ outside ±2σ band (₹{r['lo2']:.0f}–₹{r['hi2']:.0f}) — check source")
    lines.append(line)
if alerts:
    print("\n".join(alerts))
else:
    print("✅ No >2σ anomalies in the latest scrape.")
print("-"*60)
print("\n".join(lines))
print(f"\ndashboard: /home/c03rad0r/price-insight/dashboard.html")
PY
