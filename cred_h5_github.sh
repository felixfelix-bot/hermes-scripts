#!/usr/bin/env bash
# cred_h5_github.sh — CRED-H5 check (f): every PUBLIC GitHub-hosted ref tip of an
# in-scope repository is free of the retired-credential needle set.
#
# WHY (task t_f4316ea7 / CRED-H6b): check (d) (cred_h5_ngit.sh) enumerates repos
# from kind-30617 announcements and reads refs over nostr:// ONLY. A retired
# literal sitting in the tip tree of a GitHub-hosted public repo is structurally
# invisible to it. That is not hypothetical: felixfelix-bot/hermes-scripts is
# public on GitHub and its master tree carried the retired vault-master literal
# in sudo-askpass.sh until t_f17fbda0 landed 85a1087 — the guard reported
# "0 dirty refs" for that repo's entire existence because it never looked there.
# This check closes that blind spot; "0 dirty refs" is no longer an ngit-only
# statement.
#
# METHOD (read-only: NOTHING is ever written to a public host)
#   1. resolve in-scope public repos:
#        a. `gh api` visibility walk over the owner namespaces the fleet
#           publishes under (policy key github_owners), skipping private:true;
#        b. intersect with the repos THIS host holds a local clone of (plus the
#           policy's explicit github_repos pins). Rationale, declared in every
#           run: a literal can only reach a public host from a clone the fleet
#           has, so the repos the fleet has no clone of cannot receive fleet
#           content. The narrowing is never silent — the run prints
#           owner_publics=<all> excluded_no_clone=<n> next to in-scope counts;
#   2. `git ls-remote` each repo for the refs it has published;
#   3. `git fetch --depth=1` those tips into a private bare scratch repo
#      ($HOME/.hermes/state/cred-h5-githubscan — inside .hermes/state so the
#      CRED-H5 home scanner prunes it, exactly like the ngit scratch);
#   4. run the fleet literal gate per distinct tip:
#         ~/.git-hooks/cred_gate.sh --tree <sha> --repo <scratch> --quiet
#      which is the same content check the fleet pre-push hook uses. No second
#      rule set is introduced: the gate reads ~/.git-hooks/cred-needles.txt and
#      fails closed if that table is missing/corrupt.
#
# SCOPE IS DECLARED. Every run prints
#     repos_in_scope=N checked=M unreadable=K
# together with the wider accounting (owner_publics, excluded_no_clone, pins,
# refs, findings, deadline, truncated). A run that scanned 0 repos can never
# report "clean" — it exits 3 (coverage unknown).
#
# BOUNDED (the CRED-H3 shape, reused rather than inventing a new one): per-repo
# ls-remote/fetch timeouts, a per-repo ref cap, a repo cap and a whole-run
# deadline. Exhausting any bound is INCOMPLETE COVERAGE (exit 3, fail-closed),
# never a silent clean; a deliberate --no-network style skip is a different
# thing and is owned by the caller (the watchdog records it as a skip).
#
# EXIT  0 all tips clean · 1 at least one tip carries a needle
#       3 no findings but coverage incomplete (unreadable / truncated / 0 scope)
#       2 the check itself could not run (fail closed)
#
# Usage: cred_h5_github.sh [--policy P] [--scratch DIR] [--json FILE]
#                          [--ls-timeout S] [--fetch-timeout S] [--deadline S]
#                          [--max-repos N] [--max-refs N] [--refs 'refs/heads/*']
#                          [--resolve-only] [--repo-map FILE] [--local-remotes FILE]
#                          [--no-prune] [--verbose]
#   --resolve-only  resolve + print the declared scope, fetch nothing (audit mode)
#   --repo-map FILE TSV "<name>\t<url>[\t<default_branch>]" replacing steps 1a/1b.
#                   Test seam (the planted-literal harness drives a local bare repo
#                   through the real fetch+gate path); a production run never uses it.
#   --local-remotes FILE  TSV/one-per-line "owner/repo" list of local clones
#                   (test seam for step 1b; production derives it from $HOME).
set -uo pipefail

POLICY="${CRED_H5_POLICY:-$HOME/.git-hooks/cred-h5-policy.json}"
SCRATCH="${CRED_H5_GH_SCRATCH:-$HOME/.hermes/state/cred-h5-githubscan}"
GATE="${CRED_H5_GATE:-$HOME/.git-hooks/cred_gate.sh}"
LS_TIMEOUT="${CRED_H5_GH_LS_TIMEOUT:-60}"
FETCH_TIMEOUT="${CRED_H5_GH_FETCH_TIMEOUT:-300}"
DEADLINE="${CRED_H5_GH_DEADLINE:-2400}"
MAX_REPOS="${CRED_H5_GH_MAX_REPOS:-0}"      # 0 = policy value / unlimited
MAX_REFS="${CRED_H5_GH_MAX_REFS:-500}"      # per repo
REFSPEC="${CRED_H5_GH_REFSPEC:-refs/heads/*}"
JSON=""
REPO_MAP=""
LOCAL_REMOTES_FILE=""
RESOLVE_ONLY=0
PRUNE=1
VERBOSE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --policy) POLICY="${2:-}"; shift ;;
        --scratch) SCRATCH="${2:-}"; shift ;;
        --json) JSON="${2:-}"; shift ;;
        --ls-timeout) LS_TIMEOUT="${2:-}"; shift ;;
        --fetch-timeout) FETCH_TIMEOUT="${2:-}"; shift ;;
        --deadline) DEADLINE="${2:-}"; shift ;;
        --max-repos) MAX_REPOS="${2:-}"; shift ;;
        --max-refs) MAX_REFS="${2:-}"; shift ;;
        --refs) REFSPEC="${2:-}"; shift ;;
        --repo-map) REPO_MAP="${2:-}"; shift ;;
        --local-remotes) LOCAL_REMOTES_FILE="${2:-}"; shift ;;
        --resolve-only) RESOLVE_ONLY=1 ;;
        --no-prune) PRUNE=0 ;;
        --verbose) VERBOSE=1 ;;
        -h|--help) sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "cred_h5_github: unknown arg $1" >&2; exit 2 ;;
    esac
    shift
done

[ -r "$POLICY" ] || { echo "cred_h5_github: policy unreadable: $POLICY" >&2; exit 2; }
[ -x "$GATE" ] || { echo "cred_h5_github: literal gate missing: $GATE" >&2; exit 2; }
command -v git >/dev/null || { echo "cred_h5_github: git missing" >&2; exit 2; }
[ -r "$HOME/.git-hooks/cred-needles.txt" ] || {
    echo "cred_h5_github: needle table unreadable (fail-closed)" >&2; exit 2; }

if [ -z "$REPO_MAP" ]; then
    command -v gh >/dev/null || { echo "cred_h5_github: gh missing" >&2; exit 2; }
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/cred-h5-gh.XXXXXX")" || exit 2
chmod 700 "$WORK"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT

START=$SECONDS

# ---------------------------------------------------------------- resolution
# 1a. public repos under the fleet owner namespaces
OWNERS_TSV="$WORK/owners.tsv"
: > "$OWNERS_TSV"
python3 - "$POLICY" <<'PY' > "$WORK/owners.txt"
import json, sys
pol = json.load(open(sys.argv[1]))
for o in pol.get("github_owners", []):
    if isinstance(o, str) and o.strip():
        print(o.strip())
PY
OWNER_N=0; OWNER_FAIL=0; OWNER_FAIL_LIST=""
if [ -n "$REPO_MAP" ]; then
    : # scope comes from the map (test seam)
else
    while read -r owner; do
        [ -n "$owner" ] || continue
        OWNER_N=$((OWNER_N + 1))
        err="$WORK/gh-$owner.err"
        if ! timeout "$LS_TIMEOUT" gh api --paginate \
                "users/$owner/repos?type=owner&per_page=100" \
                --jq '.[] | select(.private == false) | [.full_name, ((.size // 0)|tostring), (.default_branch // "")] | @tsv' \
                >> "$OWNERS_TSV" 2> "$err"; then
            OWNER_FAIL=$((OWNER_FAIL + 1))
            OWNER_FAIL_LIST="$OWNER_FAIL_LIST $owner"
            if [ "$VERBOSE" = 1 ]; then
                printf '[gh] %-24s enumeration FAILED: %s\n' "$owner" \
                    "$(tail -1 "$err" 2>/dev/null | cut -c1-120)" >&2
            fi
        elif [ "$VERBOSE" = 1 ]; then
            printf '[gh] %-24s enumerated\n' "$owner" >&2
        fi
    done < "$WORK/owners.txt"
fi
sort -u "$OWNERS_TSV" > "$WORK/owner_publics.tsv"
OWNER_PUBLICS=$(wc -l < "$WORK/owner_publics.tsv" | tr -d ' ')

# 1b. local clones: the repos fleet content can actually be pushed into
LOCAL_LIST="$WORK/local_clones.txt"
: > "$LOCAL_LIST"
if [ -n "$REPO_MAP" ]; then
    cut -f1 "$REPO_MAP" | grep . > "$LOCAL_LIST" || true
elif [ -n "$LOCAL_REMOTES_FILE" ]; then
    grep . "$LOCAL_REMOTES_FILE" | sort -u > "$LOCAL_LIST" || true
else
    find "$HOME" -maxdepth 4 \
        \( -name node_modules -o -name .cache -o -name .bun -o -name target -o -name .venv \
           -o -name dist -o -name build -o -name snap -o -name __pycache__ -o -name .rustup \
           -o -name .cargo -o -name .npm -o -name .next -o -name .platformio -o -name .local \
           -o -name .hermes -o -name .nvm -o -name Downloads \) -prune -o \
        -name '.git' -print 2>/dev/null > "$WORK/gitdirs.txt"
    : > "$WORK/cfg.txt"
    while IFS= read -r g; do
        [ -n "$g" ] || continue
        if [ -d "$g" ]; then
            printf '%s\n' "$g/config" >> "$WORK/cfg.txt"
        elif [ -f "$g" ]; then
            d="$(sed -n 's/^gitdir: //p' "$g" | head -1)"
            [ -n "$d" ] && [ -f "$d/config" ] && printf '%s\n' "$d/config" >> "$WORK/cfg.txt"
        fi
    done < "$WORK/gitdirs.txt"
    if [ -s "$WORK/cfg.txt" ]; then
        xargs -a "$WORK/cfg.txt" -r -d '\n' grep -hoE \
            'github\.com[:/][A-Za-z0-9._-]+/[A-Za-z0-9._-]+' 2>/dev/null \
            | sed 's#github\.com[:/]##' | sed 's#\.git$##' | tr 'A-Z' 'a-z' \
            | sort -u > "$LOCAL_LIST" || true
    fi
fi
# pins always in scope (they may be pushed from another node / stale clone)
python3 - "$POLICY" <<'PY' >> "$LOCAL_LIST"
import json, sys
pol = json.load(open(sys.argv[1]))
for r in pol.get("github_repos", []):
    if isinstance(r, str) and r.strip():
        print(r.strip().lower())
PY
sort -u "$LOCAL_LIST" -o "$LOCAL_LIST"
LOCAL_N=$(wc -l < "$LOCAL_LIST" | tr -d ' ')

# in-scope = owner_publics (and, for the map mode, every entry) intersected with
# local clones; the map mode takes its entries verbatim.
# The scope file is always: <name>\t<fetch_url>\t<default_branch>
SCOPE="$WORK/scope.tsv"
: > "$SCOPE"
if [ -n "$REPO_MAP" ]; then
    awk -F'\t' 'NF>=2 && $1!="" && $2!="" {print $1"\t"$2"\t"($3==""?"":$3)}' "$REPO_MAP" \
        | sort -u > "$SCOPE"
    OWNER_PUBLICS=$(wc -l < "$SCOPE" | tr -d ' ')
else
    python3 - "$WORK/owner_publics.tsv" "$LOCAL_LIST" <<'PY' > "$WORK/joined.tsv"
import sys
pubs, local = sys.argv[1], sys.argv[2]
loc = {l.strip().lower() for l in open(local) if l.strip()}
for line in open(pubs):
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 2 or not parts[0]:
        continue
    name = parts[0]
    if name.lower() in loc:
        size = int(parts[1]) if parts[1].isdigit() else 0
        print(f"{name}\t{size}\t{parts[2] if len(parts) > 2 else ''}")
PY
    # order: pins first, then cheapest repos first (maximises coverage in budget)
    python3 - "$WORK/joined.tsv" "$POLICY" <<'PY' > "$SCOPE"
import json, sys
rows = [l.rstrip("\n").split("\t") for l in open(sys.argv[1]) if l.strip()]
pol = json.load(open(sys.argv[2]))
pins = [r.strip().lower() for r in pol.get("github_repos", []) if isinstance(r, str)]
rows.sort(key=lambda r: (0 if r[0].lower() in pins else 1,
                         int(r[1]) if len(r) > 1 and r[1].isdigit() else 0,
                         r[0].lower()))
for r in rows:
    print(f"{r[0]}\thttps://github.com/{r[0]}.git\t{r[2] if len(r) > 2 else ''}")
PY
fi
IN_SCOPE=$(wc -l < "$SCOPE" | tr -d ' ')
EXCLUDED_NO_CLONE=$(( OWNER_PUBLICS > IN_SCOPE ? OWNER_PUBLICS - IN_SCOPE : 0 ))

if [ "$VERBOSE" = 1 ]; then
    printf '[gh] owners=%s owner_publics=%s local_clones=%s in_scope=%s\n' \
        "$OWNER_N" "$OWNER_PUBLICS" "$LOCAL_N" "$IN_SCOPE" >&2
fi

if [ "$RESOLVE_ONLY" = 1 ]; then
    echo "CRED-H5 public-github scope   repos_in_scope=$IN_SCOPE checked=0 unreadable=0 (resolve-only)"
    echo "  owner_namespaces=$OWNER_N owner_publics=$OWNER_PUBLICS excluded_no_clone=$EXCLUDED_NO_CLONE local_clones=$LOCAL_N"
    [ -n "$OWNER_FAIL_LIST" ] && echo "  owner_enumeration_failed=$OWNER_FAIL_LIST"
    sed -n '1,200p' "$SCOPE" | awk -F'\t' '{printf "  scope  %s\n", $1}'
    [ "$OWNER_FAIL" -gt 0 ] && exit 3
    exit 0
fi

# ------------------------------------------------------------------ scratch
mkdir -p "$SCRATCH" || { echo "cred_h5_github: cannot create scratch $SCRATCH" >&2; exit 2; }
chmod 700 "$SCRATCH"
if [ ! -d "$SCRATCH/.git" ]; then
    git init --bare -q "$SCRATCH" >/dev/null 2>&1 || {
        echo "cred_h5_github: cannot init scratch repo" >&2; exit 2; }
fi

REFS_TOTAL=0; CHECKED=0; UNREADABLE=0; DIRTY=0; NOT_REACHED=0
REF_TRUNC=0; FINDING_ROWS=0; DEADLINE_HIT=0
: > "$WORK/findings.tsv"
REPO_IDX=0

while IFS=$'\t' read -r name url defbranch; do
    [ -n "$name" ] || continue
    [ -n "$url" ] || url="https://github.com/${name}.git"
    REPO_IDX=$((REPO_IDX + 1))
    if [ "$MAX_REPOS" -gt 0 ] && [ "$REPO_IDX" -gt "$MAX_REPOS" ]; then
        NOT_REACHED=$((NOT_REACHED + 1)); continue
    fi
    if [ "$DEADLINE" -gt 0 ] && [ $((SECONDS - START)) -ge "$DEADLINE" ]; then
        DEADLINE_HIT=1; NOT_REACHED=$((NOT_REACHED + 1)); continue
    fi
    slug="$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_')"
    pfx="refs/h5scan/$slug"
    [ "$VERBOSE" = 1 ] && printf '[gh] %-52s fetch %s\n' "$name" "$(date +%H:%M:%S)"

    ls_out=$(timeout "$LS_TIMEOUT" git ls-remote "$url" "$REFSPEC" 2>"$WORK/ls.err"); lrc=$?
    if [ "$lrc" -ne 0 ]; then
        UNREADABLE=$((UNREADABLE + 1))
        printf 'UNKNOWN\t%s\tls-remote failed rc=%s: %s\n' "$name" "$lrc" \
            "$(tail -1 "$WORK/ls.err" 2>/dev/null | tr '\n' ' ' | cut -c1-140)" >> "$WORK/findings.tsv"
        continue
    fi
    n_refs=$(printf '%s\n' "$ls_out" | grep -c . || true)
    if [ "$n_refs" -eq 0 ]; then
        UNREADABLE=$((UNREADABLE + 1))
        printf 'UNKNOWN\t%s\tno published refs (%s)\n' "$name" "$REFSPEC" >> "$WORK/findings.tsv"
        continue
    fi

    fetch_log=$(timeout "$FETCH_TIMEOUT" git --git-dir="$SCRATCH" fetch --no-tags --depth=1 --force \
        "$url" "+$REFSPEC:$pfx/*" 2>&1); frc=$?
    if [ "$frc" -ne 0 ]; then
        UNREADABLE=$((UNREADABLE + 1))
        printf 'UNKNOWN\t%s\tfetch failed rc=%s: %s\n' "$name" "$frc" \
            "$(printf '%s' "$fetch_log" | tail -1 | tr '\n' ' ' | cut -c1-140)" >> "$WORK/findings.tsv"
        continue
    fi

    mapfile -t ref_lines < <(git --git-dir="$SCRATCH" for-each-ref \
        --format='%(objectname) %(refname)' "$pfx/" 2>/dev/null)
    # default branch first, then alphabetical; dedupe by tip sha (same tree, one gate run)
    if [ -n "$defbranch" ]; then
        IFS=$'\n' ref_lines=($(printf '%s\n' "${ref_lines[@]}" \
            | awk -v d="$pfx/$defbranch" '{p=(index($2,d)==1 && length($2)==length(d))?0:1; print p" "$0}' \
            | sort -k1,1n -k3,3 | cut -d' ' -f2-))
    fi
    declared=$n_refs
    if [ "$MAX_REFS" -gt 0 ] && [ "$n_refs" -gt "$MAX_REFS" ]; then
        REF_TRUNC=$((REF_TRUNC + 1))
        ref_lines=("${ref_lines[@]:0:$MAX_REFS}")
        REFS_TOTAL=$((REFS_TOTAL + declared))
    else
        REFS_TOTAL=$((REFS_TOTAL + n_refs))
    fi

    repo_ok=1; repo_dirty=0; seen_shas=""
    for line in "${ref_lines[@]}"; do
        sha="${line%% *}"; ref="${line#* }"
        [ -n "$sha" ] || continue
        case " $seen_shas " in *" $sha "*) continue ;; esac
        seen_shas="$seen_shas $sha"
        out=$("$GATE" --tree "$sha" --repo "$SCRATCH" --quiet 2>&1); grc=$?
        case "$grc" in
            0) ;;
            1)
                repo_dirty=1
                # report rule id + sha256/12 fingerprint, and the PATH the literal
                # sits at (never the value): a finding a human cannot locate is not
                # actionable. The gate prints "committed file at <tree>: <path>".
                ids=$(printf '%s' "$out" \
                    | sed -n 's/.*id=\([^ ]*\).*sha256\/12=\([^ ]*\).*/\1:\2/p' | sort -u | tr '\n' ' ')
                paths=$(printf '%s' "$out" \
                    | sed -n "s/^ *committed file at ${sha}: //p" | sort -u | head -5 | tr '\n' ',')
                printf 'FINDING\t%s\t%s %s paths=%s ids=%s\n' "$name" "${ref#$pfx/}" "${sha:0:8}" \
                    "${paths%,}" "$ids" >> "$WORK/findings.tsv"
                FINDING_ROWS=$((FINDING_ROWS + 1))
                ;;
            *)
                repo_ok=0
                printf 'UNKNOWN\t%s\tgate unusable on %s rc=%s\n' "$name" "${ref#$pfx/}" "$grc" \
                    >> "$WORK/findings.tsv"
                ;;
        esac
    done
    if [ "$repo_ok" -eq 0 ]; then
        UNREADABLE=$((UNREADABLE + 1))
    else
        CHECKED=$((CHECKED + 1))
        [ "$repo_dirty" -eq 1 ] && DIRTY=$((DIRTY + 1))
    fi
    if [ "$DEADLINE" -gt 0 ] && [ $((SECONDS - START)) -ge "$DEADLINE" ]; then
        DEADLINE_HIT=1
    fi
done < "$SCOPE"

# ------------------------------------------------------------- scratch prune
if [ "$PRUNE" = 1 ]; then
    timeout 300 git --git-dir="$SCRATCH" gc --quiet --prune=now >/dev/null 2>&1 || true
fi
SCRATCH_MB=0
[ -d "$SCRATCH" ] && SCRATCH_MB=$(( $(du -sk "$SCRATCH" 2>/dev/null | cut -f1) / 1024 ))

f_files=$(grep -c '^FINDING' "$WORK/findings.tsv" 2>/dev/null || true)
u_lines=$(grep -c '^UNKNOWN' "$WORK/findings.tsv" 2>/dev/null || true)
[ -n "$f_files" ] || f_files=0
[ -n "$u_lines" ] || u_lines=0

INCOMPLETE=0
[ "$IN_SCOPE" -eq 0 ] && INCOMPLETE=1
[ "$UNREADABLE" -gt 0 ] && INCOMPLETE=1
[ "$NOT_REACHED" -gt 0 ] && INCOMPLETE=1
[ "$REF_TRUNC" -gt 0 ] && INCOMPLETE=1
[ "$DEADLINE_HIT" -eq 1 ] && INCOMPLETE=1
# an owner whose enumeration failed may have dropped repos from scope silently:
# that is incomplete coverage, never a clean run.
[ "$OWNER_FAIL" -gt 0 ] && INCOMPLETE=1

echo "CRED-H5 public-github scan   repos_in_scope=$IN_SCOPE checked=$CHECKED unreadable=$UNREADABLE"
echo "  accounting: owner_namespaces=$OWNER_N owner_publics=$OWNER_PUBLICS excluded_no_clone=$EXCLUDED_NO_CLONE local_clones=$LOCAL_N"
echo "  refs=$REFS_TOTAL findings=$f_files dirty_repos=$DIRTY not_reached=$NOT_REACHED ref_truncated_repos=$REF_TRUNC deadline_hit=$DEADLINE_HIT scratch_mb=$SCRATCH_MB refs_spec=$REFSPEC elapsed_s=$((SECONDS - START))"
[ -n "$OWNER_FAIL_LIST" ] && echo "  owner_enumeration_failed=$OWNER_FAIL_LIST"
if [ -s "$WORK/findings.tsv" ]; then
    sed -n '1,40p' "$WORK/findings.tsv" | while IFS=$'\t' read -r kind nm detail; do
        printf '  %-8s %-52s %s\n' "$kind" "$nm" "$detail"
    done
fi
if [ "$IN_SCOPE" -eq 0 ]; then
    echo "github verdict: COVERAGE UNKNOWN (resolved 0 repos — a sweep with no scope must never print clean)"
elif [ "$f_files" -gt 0 ]; then
    echo "github verdict: HITS PRESENT"
elif [ "$INCOMPLETE" -eq 1 ]; then
    echo "github verdict: UNKNOWN COVERAGE"
else
    echo "github verdict: CLEAN (0 hits on every in-scope public GitHub tip)"
fi

if [ -n "$JSON" ]; then
    python3 - "$WORK/findings.tsv" "$JSON" "$IN_SCOPE" "$CHECKED" "$DIRTY" "$f_files" \
        "$UNREADABLE" "$REFS_TOTAL" "$OWNER_N" "$OWNER_PUBLICS" "$EXCLUDED_NO_CLONE" \
        "$LOCAL_N" "$NOT_REACHED" "$REF_TRUNC" "$DEADLINE_HIT" "$SCRATCH_MB" "$REFSPEC" \
        "$OWNER_FAIL" <<'PY'
import json, sys
(tsv, out, scope, checked, dirty, files, unread, refs, owners, pubs, excluded,
 local, not_reached, trunc, dl, scratch_mb, refspec, owner_fail) = sys.argv[1:19]
rows = []
for line in open(tsv, errors="replace"):
    parts = line.rstrip("\n").split("\t")
    if len(parts) >= 2:
        rows.append({"kind": parts[0], "repo": parts[1],
                     "detail": parts[2] if len(parts) > 2 else ""})
json.dump({"repos": int(scope), "checked": int(checked), "dirty_repos": int(dirty),
           "refs_with_needles": int(files), "unreadable": int(unread),
           "refs": int(refs), "refs_spec": refspec,
           "coverage": {"owner_namespaces": int(owners), "owner_publics": int(pubs),
                        "excluded_no_clone": int(excluded), "local_clones": int(local),
                        "not_reached": int(not_reached), "ref_truncated_repos": int(trunc),
                        "deadline_hit": dl == "1", "scratch_mb": int(scratch_mb),
                        "owner_enumeration_failed": int(owner_fail)},
           "findings": rows}, open(out, "w"), indent=1)
PY
fi

[ "$f_files" -gt 0 ] && exit 1
[ "$INCOMPLETE" -eq 1 ] && exit 3
exit 0
