#!/usr/bin/env bash
# cred_h5_ngit.sh — CRED-H5 check (d): every PUBLIC ngit head is free of the
# retired-credential needle set.
#
# Method: fetch refs/heads/* of each configured public ngit repository
# (nostr:// remote helper, relay-mediated, no credentials) into a private
# scratch repository, then run the fleet literal gate (~/.git-hooks/cred_gate.sh
# --tree) over every fetched tip. The gate prints rule ids + sha256/12
# fingerprints only, so no literal ever reaches this output.
#
# The scratch repo keeps its objects between runs (incremental fetches), holds
# no working tree, and lives under .hermes/state (pruned by the home scanner).
#
# Exit: 0 all repos clean · 1 at least one ref carries a needle · 3 no findings
#       but at least one repo could not be read (fail-closed: absence of
#       findings must never be reported as clean coverage).
#
# Usage: cred_h5_ngit.sh [--policy P] [--scratch DIR] [--timeout SEC] [--json F]
#                        [--refs 'refs/heads/*'] [--verbose]
set -uo pipefail

POLICY="${CRED_H5_POLICY:-$HOME/.git-hooks/cred-h5-policy.json}"
SCRATCH="${CRED_H5_NGIT_SCRATCH:-$HOME/.hermes/state/cred-h5-ngitscan}"
GATE="$HOME/.git-hooks/cred_gate.sh"
TIMEOUT="${CRED_H5_NGIT_TIMEOUT:-300}"
JSON=""
REFSPEC='refs/heads/*'
VERBOSE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --policy) POLICY="$2"; shift ;;
        --scratch) SCRATCH="$2"; shift ;;
        --timeout) TIMEOUT="$2"; shift ;;
        --json) JSON="$2"; shift ;;
        --refs) REFSPEC="$2"; shift ;;
        --verbose) VERBOSE=1 ;;
        *) echo "cred_h5_ngit: unknown arg $1" >&2; exit 2 ;;
    esac
    shift
done

[ -r "$POLICY" ] || { echo "cred_h5_ngit: policy unreadable: $POLICY" >&2; exit 2; }
[ -x "$GATE" ] || { echo "cred_h5_ngit: literal gate missing: $GATE" >&2; exit 2; }
command -v git >/dev/null || { echo "cred_h5_ngit: git missing" >&2; exit 2; }

mkdir -p "$SCRATCH"
chmod 700 "$SCRATCH"
cd "$SCRATCH" || exit 2
[ -d .git ] || git init -q .
# a work tree is required by ngit's remote helper
touch .gitkeep

REPOS_TSV=$(python3 - "$POLICY" <<'PY'
import json, sys
pol = json.load(open(sys.argv[1]))
for r in pol.get("ngit_repos", []):
    print(f"{r['name']}\t{r['url']}")
PY
) || { echo "cred_h5_ngit: cannot read ngit_repos from policy" >&2; exit 2; }

TOTAL=0; DIRTY=0; UNKNOWN=0
FINDINGS=""
: > "$SCRATCH/findings.tsv"
while IFS=$'\t' read -r name url; do
    [ -n "$name" ] || continue
    TOTAL=$((TOTAL + 1))
    refpfx="refs/h5scan/$name"
    printf '[ngit] %-36s fetch %s\n' "$name" "$(date +%H:%M:%S)"
    fetch_log=$(timeout "$TIMEOUT" git fetch --no-tags --force "$url" \
        "+$REFSPEC:$refpfx/*" 2>&1); frc=$?
    if [ "$frc" -ne 0 ]; then
        UNKNOWN=$((UNKNOWN + 1))
        FINDINGS="${FINDINGS}UNKNOWN\t$name\tfetch failed rc=$frc: $(printf '%s' "$fetch_log" | tail -2 | tr '\n' ' ')\n"
        continue
    fi
    refs=$(git for-each-ref --format='%(refname) %(objectname)' "$refpfx/" 2>/dev/null)
    if [ -z "$refs" ]; then
        UNKNOWN=$((UNKNOWN + 1))
        FINDINGS="${FINDINGS}UNKNOWN\t$name\tno refs published\n"
        continue
    fi
    n_refs=0; n_find=0
    while read -r ref sha; do
        [ -n "$sha" ] || continue
        n_refs=$((n_refs + 1))
        out=$("$GATE" --tree "$sha" --repo "$SCRATCH" --quiet 2>&1); rc=$?
        case "$rc" in
            0) ;;
            1)
                n_find=$((n_find + 1))
                ids=$(printf '%s' "$out" | sed -n 's/.*id=\([^ ]*\).*sha256\/12=\([^ ]*\).*/\1:\2/p' | sort -u | tr '\n' ' ')
                FINDINGS="${FINDINGS}FINDING\t$name\t${ref#$refpfx/} ${sha:0:8} ids=$ids\n"
                ;;
            *)
                UNKNOWN=$((UNKNOWN + 1))
                FINDINGS="${FINDINGS}UNKNOWN\t$name\tgate unusable on ${ref#$refpfx/} rc=$rc\n"
                ;;
        esac
    done <<< "$refs"
    [ "$VERBOSE" = 1 ] && printf '[ngit] %-36s refs=%s dirty=%s\n' "$name" "$n_refs" "$n_find"
    [ "$n_find" -gt 0 ] && DIRTY=$((DIRTY + 1))
done <<< "$REPOS_TSV"

printf '%b' "$FINDINGS" > "$SCRATCH/findings.tsv"
f_files=$(grep -c '^FINDING' "$SCRATCH/findings.tsv" || true)
u_lines=$(grep -c '^UNKNOWN' "$SCRATCH/findings.tsv" || true)

echo "CRED-H5 public-ngit scan   repos=${TOTAL}  dirty_repos=${DIRTY}  refs_with_needles=${f_files}  unreadable=${u_lines}"
if [ -s "$SCRATCH/findings.tsv" ]; then
    sed -n '1,40p' "$SCRATCH/findings.tsv" | while IFS=$'\t' read -r kind name detail; do
        printf '  %-8s %-36s %s\n' "$kind" "$name" "$detail"
    done
fi
if [ "$f_files" -eq 0 ] && [ "$u_lines" -eq 0 ]; then
    echo "ngit verdict: CLEAN (0 hits on every public ngit head)"
else
    echo "ngit verdict: $( [ "$f_files" -gt 0 ] && printf 'HITS PRESENT' || printf 'UNKNOWN COVERAGE' )"
fi

if [ -n "$JSON" ]; then
    python3 - "$SCRATCH/findings.tsv" "$JSON" "$TOTAL" "$DIRTY" "$f_files" "$u_lines" <<'PY'
import json, sys
tsv, out, total, dirty, files_, unknown = sys.argv[1:7]
rows = []
for line in open(tsv, errors="replace"):
    parts = line.rstrip("\n").split("\t")
    if len(parts) >= 2:
        rows.append({"kind": parts[0], "repo": parts[1], "detail": parts[2] if len(parts) > 2 else ""})
json.dump({"repos": int(total), "dirty_repos": int(dirty), "refs_with_needles": int(files_),
           "unreadable": int(unknown), "findings": rows}, open(out, "w"), indent=1)
PY
fi

[ "$f_files" -gt 0 ] && exit 1
[ "$u_lines" -gt 0 ] && exit 3
exit 0
