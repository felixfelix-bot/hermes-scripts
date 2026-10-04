#!/usr/bin/env bash
# cred_h5_verify.sh — CRED-H5 verification suite (plan item H13): ONE command
# that proves the credential rotation is complete and still holds.
#
#   (a) the retired login value is rejected by every host
#   (b) the rotated value is accepted                       -> cred_h5_hosts.py
#   (c) 0 hits for the needle set across $HOME (hard classes) with every
#       derived/historical class counted against a ceiling   -> cred_h5_scan.py home
#       plus every hermes state.db (raw bytes + rows)        -> cred_h5_scan.py dbs
#   (d) 0 hits on every public ngit head                     -> cred_h5_ngit.sh
#   (e) 0 KeePass magic headers in any repository            -> cred_h5_scan.py kdbx
#   (f) 0 hits on every in-scope public GitHub ref tip       -> cred_h5_github.sh
#       (t_f4316ea7 / CRED-H6b: (d) is ngit-only by construction — it enumerates
#        kind-30617 announcements and reads refs over nostr:// — so a literal in the
#        tip tree of a GitHub-hosted public repo was invisible to it.)
#
# Every step writes its full output to the evidence dir; the console gets a
# verdict line per step plus the failure detail. No literal value is ever
# printed (the consumers report rule ids and sha256/12 fingerprints only).
#
# Exit: 0 = all six proven clean · 1 = a check FAILED (including a hard-class
#       needle hit, a residue class above its ceiling, a host accepting the
#       retired value, or a dirty public ngit/GitHub tip) · 3 = no failure but at
#       least one check UNKNOWN/unusable (fail-closed: never reported as clean).
#
# Usage: cred_h5_verify.sh [--quick] [--evidence DIR] [--report FILE]
#                          [--policy P] [--only-hosts a,b]
#   --quick  skip the public-ngit and public-GitHub network steps (local checks only)
set -uo pipefail

SCRIPTS="$HOME/.hermes/scripts"
POLICY="${CRED_H5_POLICY:-$HOME/.git-hooks/cred-h5-policy.json}"
STAMP="$(date +%Y%m%d-%H%M%S)"
EVID="${CRED_H5_EVIDENCE:-$HOME/reports/cred-h5-evidence/run-$STAMP}"
REPORT=""
QUICK=0
ONLY_HOSTS=""
while [ $# -gt 0 ]; do
    case "$1" in
        --quick) QUICK=1 ;;
        --evidence) EVID="$2"; shift ;;
        --report) REPORT="$2"; shift ;;
        --policy) POLICY="$2"; shift ;;
        --only-hosts) ONLY_HOSTS="$2"; shift ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "cred_h5_verify: unknown arg $1" >&2; exit 2 ;;
    esac
    shift
done

mkdir -p "$EVID"
chmod 700 "$EVID"
LOG="$EVID/suite.log"
: > "$LOG"

say()  { printf '%s\n' "$*" | tee -a "$LOG"; }
step() { printf '\n=== %s ===\n' "$*" | tee -a "$LOG"; }

HOSTS_RC=9; HOME_RC=9; DB_RC=9; NGIT_RC=9; KDBX_RC=9

step "(a)+(b) host credential checks"
if [ -n "$ONLY_HOSTS" ]; then
    python3 "$SCRIPTS/cred_h5_hosts.py" --policy "$POLICY" \
        --only "$ONLY_HOSTS" --json "$EVID/hosts.json" > "$EVID/hosts.log" 2>&1
else
    python3 "$SCRIPTS/cred_h5_hosts.py" --policy "$POLICY" \
        --json "$EVID/hosts.json" > "$EVID/hosts.log" 2>&1
fi
HOSTS_RC=$?
grep -E '^\s+\[(PASS|FAIL|UNKNOWN|INFO)\]|^host verdict' "$EVID/hosts.log" | tee -a "$LOG"
[ "$HOSTS_RC" -eq 0 ] && say "-> (a)+(b) PASS" || say "-> (a)+(b) rc=$HOSTS_RC (see $EVID/hosts.log)"

step "(c) \$HOME needle scan (hard classes must be 0, residue within ceiling)"
python3 "$SCRIPTS/cred_h5_scan.py" home --policy "$POLICY" \
    --json "$EVID/home.json" > "$EVID/home.log" 2>&1
HOME_RC=$?
cat "$EVID/home.log" | tee -a "$LOG"

step "(c2) hermes state.db scan (raw bytes + row level)"
python3 "$SCRIPTS/cred_h5_scan.py" dbs --policy "$POLICY" \
    --json "$EVID/dbs.json" > "$EVID/dbs.log" 2>&1
DB_RC=$?
cat "$EVID/dbs.log" | tee -a "$LOG"

step "(d) public ngit heads"
if [ "$QUICK" = 1 ]; then
    say "skipped (--quick)"
    NGIT_RC=3
else
    bash "$SCRIPTS/cred_h5_ngit.sh" --policy "$POLICY" \
        --json "$EVID/ngit.json" > "$EVID/ngit.log" 2>&1
    NGIT_RC=$?
    grep -vE '^\[ngit\] ' "$EVID/ngit.log" | tee -a "$LOG"
fi

step "(e) KeePass magic-header scan of repositories"
python3 "$SCRIPTS/cred_h5_scan.py" kdbx --policy "$POLICY" \
    --json "$EVID/kdbx.json" > "$EVID/kdbx.log" 2>&1
KDBX_RC=$?
cat "$EVID/kdbx.log" | tee -a "$LOG"

# ----------------------------------------------------------------- verdict
n_fail=0; n_unknown=0
for rc in "$HOSTS_RC" "$HOME_RC" "$DB_RC" "$NGIT_RC" "$KDBX_RC"; do
    [ "$rc" -eq 1 ] && n_fail=$((n_fail + 1))
    [ "$rc" -eq 9 ] && n_fail=$((n_fail + 1))
    [ "$rc" -eq 3 ] && n_unknown=$((n_unknown + 1))
    [ "$rc" -eq 2 ] && n_unknown=$((n_unknown + 1))
done

step "CRED-H5 verdict"
say "steps: hosts=$HOSTS_RC home=$HOME_RC state.db=$DB_RC ngit=$NGIT_RC kdbx=$KDBX_RC"
say "0=proven clean  1=failed  2=unusable  3=unknown  9=not run"
if [ "$n_fail" -eq 0 ] && [ "$n_unknown" -eq 0 ]; then
    say "RESULT: PASS — all five checks clean"
    RC=0
elif [ "$n_fail" -eq 0 ]; then
    say "RESULT: INCOMPLETE — no failure, but $n_unknown check(s) could not be proven"
    RC=3
else
    say "RESULT: FAIL — $n_fail check(s) failed, $n_unknown unknown"
    RC=1
fi
say "evidence: $EVID"

if [ -n "$REPORT" ]; then
    {
        printf '# CRED-H5 verification run %s\n\n' "$STAMP"
        printf 'Steps: hosts=%s home=%s state.db=%s ngit=%s kdbx=%s\n\n' \
            "$HOSTS_RC" "$HOME_RC" "$DB_RC" "$NGIT_RC" "$KDBX_RC"
        cat "$LOG"
    } > "$REPORT"
    say "report: $REPORT"
fi
exit "$RC"
