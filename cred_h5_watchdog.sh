#!/usr/bin/env bash
# cred_h5_watchdog.sh — CRED-H5 nightly regression guard (plan item H14).
#
# Scope (the DoD): alert on ANY reappearance of a retired credential literal
#   * anywhere in $HOME that is not a counted residue class      (hard classes)
#   * in any hermes state.db (raw bytes or row level, incl. FTS)
#   * on any public ngit head                    (check (d), cred_h5_ngit.sh)
#   * on any public GitHub tip in scope          (check (f), cred_h5_github.sh)
# plus the KeePass magic-header sweep, and the host rejection probe (a host that
# starts ACCEPTING the retired value is a live exposure and always alerts).
#
# (f) exists because (d) is ngit-only by construction: it enumerates kind-30617
# announcements and reads refs over nostr://, so a literal in the tip tree of a
# GitHub-hosted public repo is invisible to it (t_f4316ea7 / CRED-H6b: that is
# how felixfelix-bot/hermes-scripts carried the retired vault-master literal
# through three sweeps until t_f17fbda0 landed 85a1087).
#
# Silent when clean: no stdout at all, so a no_agent cron tick delivers nothing.
# One alert block when dirty (stdout), plus a best-effort Signal/chat bridge POST
# so a human sees it even though CLI cron delivery is local-only.
#
# Repeat suppression: the alert text is hashed; an identical finding set stays
# silent for up to 7 consecutive nights (then re-alerts as a reminder). Any new
# or changed finding alerts immediately. --force ignores suppression.
#
# Exit: 0 = silent when clean, or an alert was delivered (the run succeeded);
#       2 = the watchdog itself could not run (fail-loud, never silent).
#
# Usage: cred_h5_watchdog.sh [--policy P] [--state F] [--verbose] [--force]
#                            [--no-network]
#   --no-network  skip the public-ngit and public-github steps. Each step is
#                 recorded UNKNOWN (a skipped step is never reported clean) but a
#                 deliberate skip is NOT a coverage finding: it does not raise an
#                 alert by itself (the run-437 defect must not regress).
#   --force       ignore the 7-night repeat suppression
#   --verbose     print a summary line even when clean (the cron tick does not
#                 pass this, so a clean tick stays byte-empty)
set -uo pipefail

SCRIPTS="$HOME/.hermes/scripts"
POLICY="${CRED_H5_POLICY:-$HOME/.git-hooks/cred-h5-policy.json}"
STATE="${CRED_H5_WATCHDOG_STATE:-$HOME/.hermes/state/cred-h5-watchdog.json}"
EVID_ROOT="$HOME/reports/cred-h5-evidence"
STAMP="$(date +%Y%m%d-%H%M%S)"
VERBOSE=0; FORCE=0; NET=1
while [ $# -gt 0 ]; do
    case "$1" in
        --policy) POLICY="$2"; shift ;;
        --state) STATE="$2"; shift ;;
        --verbose) VERBOSE=1 ;;
        --force) FORCE=1 ;;
        --no-network) NET=0 ;;
        -h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "cred_h5_watchdog: unknown arg $1" >&2; exit 2 ;;
    esac
    shift
done

need() { command -v "$1" >/dev/null 2>&1; }
for t in python3 git grep; do
    need "$t" || { echo "CRED-H5 WATCHDOG BROKEN: $t missing"; exit 2; }
done
[ -r "$POLICY" ] || { echo "CRED-H5 WATCHDOG BROKEN: policy unreadable: $POLICY"; exit 2; }
[ -r "$HOME/.git-hooks/cred-needles.txt" ] || {
    echo "CRED-H5 WATCHDOG BROKEN: needle table unreadable (fail-closed)"; exit 2; }

EVID="$EVID_ROOT/watchdog-$STAMP"
mkdir -p "$EVID"; chmod 700 "$EVID"

# ------------------------------------------------------------------ checks
python3 "$SCRIPTS/cred_h5_scan.py" home --policy "$POLICY" \
    --json "$EVID/home.json" > "$EVID/home.log" 2>&1; HOME_RC=$?
python3 "$SCRIPTS/cred_h5_scan.py" dbs --policy "$POLICY" \
    --json "$EVID/dbs.json" > "$EVID/dbs.log" 2>&1;  DB_RC=$?
python3 "$SCRIPTS/cred_h5_scan.py" kdbx --policy "$POLICY" \
    --json "$EVID/kdbx.json" > "$EVID/kdbx.log" 2>&1; KDBX_RC=$?
if [ "$NET" = 1 ]; then
    bash "$SCRIPTS/cred_h5_ngit.sh" --policy "$POLICY" \
        --json "$EVID/ngit.json" --scratch "$HOME/.hermes/state/cred-h5-ngitscan" \
        > "$EVID/ngit.log" 2>&1; NGIT_RC=$?
    bash "$SCRIPTS/cred_h5_github.sh" --policy "$POLICY" \
        --json "$EVID/github.json" --scratch "$HOME/.hermes/state/cred-h5-githubscan" \
        > "$EVID/github.log" 2>&1; GH_RC=$?
else
    # Explicit operator/self-test skip. Recorded as UNKNOWN for the STEP verdict (a
    # skipped step must never be reported as clean) but it is NOT a coverage finding
    # and must not by itself raise an alert - the summary is told via $NET.
    NGIT_RC=3; echo "skipped (--no-network): not a coverage finding" > "$EVID/ngit.log"
    GH_RC=3;   echo "skipped (--no-network): not a coverage finding" > "$EVID/github.log"
fi
# Host probe: a host accepting the retired value always alerts; an unreachable
# host is recorded but does not by itself wake a human (documented tolerance).
python3 "$SCRIPTS/cred_h5_hosts.py" --policy "$POLICY" \
    --json "$EVID/hosts.json" > "$EVID/hosts.log" 2>&1; HOSTS_RC=$?

# ------------------------------------------------------------------ summary
SUMMARY=$(python3 - "$EVID" "$HOME_RC" "$DB_RC" "$KDBX_RC" "$NGIT_RC" "$GH_RC" "$HOSTS_RC" "$NET" <<'PY'
import json, os, sys
evid = sys.argv[1]
home_rc, db_rc, kdbx_rc, ngit_rc, gh_rc, hosts_rc = map(int, sys.argv[2:8])
net = sys.argv[8] == "1"
lines = []

def load(name):
    p = os.path.join(evid, name)
    try:
        return json.load(open(p))
    except Exception:
        return {}

h = load("home.json")
hard_files = h.get("hard_files", 0)
hard_occ = h.get("hard_occurrences", 0)
hard_classes = {k: v for k, v in (h.get("classes") or {}).items()
                if v.get("tier") == "hard"}
over = h.get("residue_over_ceiling") or []
d = load("dbs.json")
ng = load("ngit.json")
ghp = load("github.json")
hosts = load("hosts.json")

if home_rc == 1:
    if hard_files:
        lines.append(f"HOME: {hard_files} file(s) / {hard_occ} occurrence(s) in HARD classes "
                     f"({', '.join(sorted(hard_classes))})")
        for cls, info in sorted(hard_classes.items()):
            for f in (info.get("files_listed") or [])[:10]:
                lines.append(f"  - {f}")
    if over:
        lines.append(f"HOME: residue class(es) above ceiling: {', '.join(over)}")
elif home_rc >= 2:
    lines.append(f"HOME: scan did not run (rc={home_rc})")

if db_rc == 1:
    lines.append("STATE.DB: residue in raw bytes or rows (see stderr/report)")
elif db_rc >= 2:
    lines.append(f"STATE.DB: scan did not run (rc={db_rc})")

if ngit_rc == 1:
    for f in (ng.get("findings") or []):
        if f.get("kind") == "FINDING":
            lines.append(f"NGIT: {f['repo']} {f['detail']}")
elif ngit_rc == 3:
    if net:
        lines.append(f"NGIT: coverage UNKNOWN ({ng.get('unreadable', '?')} repo(s) unreadable)")
    # net == 0: an explicit --no-network skip is not a finding, so it stays silent.
elif ngit_rc >= 2:
    lines.append(f"NGIT: scan did not run (rc={ngit_rc})")

# check (f): the GitHub-side tip-tree sweep. rc=3 on a REAL run means coverage was
# incomplete (unreadable repo / deadline / truncation / empty scope) and MUST alert:
# a sweep that did not finish looking is not a clean sweep (t_f4316ea7). A deliberate
# --no-network skip (net == 0) is not a finding and stays silent.
if gh_rc == 1:
    for f in (ghp.get("findings") or []):
        if f.get("kind") == "FINDING":
            lines.append(f"GITHUB: {f['repo']} {f['detail']}")
elif gh_rc == 3:
    if net:
        cov = ghp.get("coverage") or {}
        lines.append("GITHUB: coverage UNKNOWN "
                     f"(repos_in_scope={ghp.get('repos', '?')} checked={ghp.get('checked', '?')} "
                     f"unreadable={ghp.get('unreadable', '?')} not_reached={cov.get('not_reached', '?')} "
                     f"deadline_hit={cov.get('deadline_hit', '?')} "
                     f"owner_enumeration_failed={cov.get('owner_enumeration_failed', '?')})")
elif gh_rc >= 2:
    lines.append(f"GITHUB: scan did not run (rc={gh_rc})")

if kdbx_rc == 1:
    lines.append("KDBX: KeePass magic header present in a repository (see kdbx.log)")
elif kdbx_rc >= 2:
    lines.append(f"KDBX: scan did not run (rc={kdbx_rc})")

if hosts and hosts.get("fail"):
    for r in hosts.get("results", []):
        if r.get("status") == "FAIL":
            lines.append(f"HOST: {r.get('check')} — {r.get('detail')}")

verb = [l for l in lines if l]
print("|".join(verb))
PY
) || { echo "CRED-H5 WATCHDOG BROKEN: summary step failed"; exit 2; }

# ------------------------------------------------------------------ verdict
if [ -z "$SUMMARY" ]; then
    if [ "$VERBOSE" = 1 ]; then
        echo "CRED-H5 watchdog: clean — hard=0, residue within ceilings, state.db clean, kdbx clean, no host accepting the retired value"
        [ "$NET" = 0 ] && echo "  (ngit + github steps skipped by --no-network: not assessed in this run)"
        # coverage is DECLARED, not implied: print what each network step actually
        # looked at, so "clean" is never a statement about an empty scan
        python3 - "$EVID" <<'PY'
import json, os, sys
evid = sys.argv[1]
for label, fn in (("ngit", "ngit.json"), ("github", "github.json")):
    try:
        d = json.load(open(os.path.join(evid, fn)))
    except Exception:
        continue
    if label == "ngit":
        print(f"  {label} coverage: repos={d.get('repos','?')} dirty_repos={d.get('dirty_repos','?')} "
              f"unreadable={d.get('unreadable','?')}")
    else:
        c = d.get("coverage") or {}
        print(f"  {label} coverage: repos_in_scope={d.get('repos','?')} checked={d.get('checked','?')} "
              f"unreadable={d.get('unreadable','?')} refs={d.get('refs','?')} "
              f"owner_publics={c.get('owner_publics','?')} excluded_no_clone={c.get('excluded_no_clone','?')} "
              f"scratch_mb={c.get('scratch_mb','?')}")
PY
        echo "evidence: $EVID"
        [ "$HOME_RC" -eq 2 ] && echo "note: home scan rc=2"
    fi
    # keep the state file's first_seen bookkeeping for the clean case
    python3 - "$STATE" "$EVID" <<'PY'
import json, os, sys, time
state_path, evid = sys.argv[1], sys.argv[2]
try:
    st = json.load(open(state_path))
except Exception:
    st = {}
st["last_clean"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
st["last_evidence"] = evid
st["repeat"] = 0
os.makedirs(os.path.dirname(state_path), exist_ok=True)
tmp = state_path + ".tmp"
json.dump(st, open(tmp, "w"), indent=1)
os.replace(tmp, state_path)
PY
    exit 0
fi

# dirty: suppress an unchanged finding set for up to 7 nights
HASH=$(printf '%s' "$SUMMARY" | sha256sum | cut -c1-16)
ACTION=$(python3 - "$STATE" "$HASH" "$FORCE" <<'PY'
import json, os, sys
state_path, digest, force = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
try:
    st = json.load(open(state_path))
except Exception:
    st = {}
same = st.get("last_hash") == digest
st["repeat"] = int(st.get("repeat", 0)) + 1 if same else 1
st["last_hash"] = digest
st["last_alert_set"] = st.get("last_alert_set", "") if same else ""
os.makedirs(os.path.dirname(state_path), exist_ok=True)
tmp = state_path + ".tmp"
json.dump(st, open(tmp, "w"), indent=1)
os.replace(tmp, state_path)
if force or not same or st["repeat"] % 7 == 0:
    print("ALERT")
else:
    print(f"SUPPRESS (repeat {st['repeat']})")
PY
)

if [ "${ACTION%% *}" = "ALERT" ]; then
    TS=$(date '+%Y-%m-%d %H:%M:%S%z')
    printf 'CRED-H5 WATCHDOG ALERT %s — retired credential literal reappeared\n' "$TS"
    printf '%s\n' "$SUMMARY" | tr '|' '\n' | sed 's/^/  /'
    echo "  evidence: $EVID"
    echo "  rule: plan H13/H14 · policy $(basename "$POLICY") · this is a regression of the CRED rotation"
    if [ "${CRED_H5_WATCHDOG_SIGNAL:-1}" = "1" ] && need curl; then
        MSG="CRED-H5 WATCHDOG: retired credential literal reappeared ($TS). $(printf '%s' "$SUMMARY" | tr '|' '; ' | cut -c1-700) — evidence: $EVID"
        PAYLOAD=$(python3 - "$MSG" <<'PY'
import json, sys
print(json.dumps({"jsonrpc": "2.0", "method": "send",
                  "params": {"message": sys.argv[1]}, "id": 1}))
PY
)
        curl -s --max-time 8 -X POST http://localhost:8080/api/v1/rpc \
            -H 'Content-Type: application/json' -d "$PAYLOAD" >/dev/null 2>&1 || true
    fi
    exit 0
fi

# suppressed: last week's identical finding set
python3 - "$STATE" "$EVID" <<'PY'
import json, os, sys, time
state_path, evid = sys.argv[1], sys.argv[2]
st = json.load(open(state_path))
st["last_evidence"] = evid
st["last_state"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
tmp = state_path + ".tmp"
json.dump(st, open(tmp, "w"), indent=1)
os.replace(tmp, state_path)
PY
exit 0
