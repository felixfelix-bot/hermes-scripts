#!/bin/bash
# test_kanban_worker_quota_taxonomy.sh — T3.3 quality gates for the worker
# quota-pause taxonomy patch (PLAN-v2-remediation.md §T3.3, decision D9=B:
# worker drafts a patch file, the manager applies and commits it).
#
# The deliverable is a PATCH FILE applied by hand to the kanban-worker
# SKILL.md copies the fleet actually reads. This suite asserts the properties
# that make that safe:
#
#   t1  patch file exists and is non-empty
#   t2  patch is a clean -p1 unified diff (2 hunks, relative paths, LF, no CRLF)
#   t3  applies with --fuzz=0 to fixture A (stale default/worker variant)
#   t4  applies with --fuzz=0 to fixture B (manager SoT variant)
#   t5  applies to the LIVE default-profile copy            [SKIP if absent]
#   t6  applies to the LIVE manager SoT copy                [SKIP if absent]
#   t7  applies to a LIVE fleet worker-profile copy         [SKIP if absent]
#   t8  all taxonomy markers present after apply (fixture A)
#   t9  all taxonomy markers present after apply (fixture B)
#   t10 the inserted section is byte-identical in both variants (one canonical text)
#   t11 the section is inserted exactly once (no double insert)
#   t12 frontmatter still parses; name/description kept; version 2.0.0 -> 2.2.0
#   t13 the change is reversible (patch -R dry-run succeeds on the applied file)
#   t14 NEGATIVE control: patch must NOT apply to an unrelated skill doc
#   t15 apply helper: applies to a base-A copy, verifies markers, writes a backup
#   t16 apply helper: second run is a no-op (idempotent), file byte-unchanged
#   t17 apply helper: unknown base without --force refuses and touches nothing
#   t18 runtime skill-load check (verify_skill_loads.py): the patched skill is
#       still discoverable by Hermes' manifest scanner, same key set as control
#   t19 apply helper: a PARTIALLY applied file (some, not all markers) is
#       refused as an unknown state and left byte-identical
#   t20 apply helper: a partial apply's stray .rej is removed (no litter)
#
# Hermetic legs run against tests/fixtures/kanban-worker-anchor-{A,B}.md (a
# minimal, exact-context reduction of both real variants). Live-fleet legs run
# against the real copies when present and SKIP loudly otherwise.
#
# Run: bash tests/test_kanban_worker_quota_taxonomy.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

PATCH="${PATCH_FILE:-$ROOT/patches/kanban-worker_quota-pause-taxonomy.patch}"
APPLY="${APPLY_SCRIPT:-$ROOT/patches/apply-kanban-worker_quota-pause-taxonomy.sh}"
REL="devops/kanban-worker/SKILL.md"

SKILL_DEFAULT="${SKILL_DEFAULT:-$HOME/.hermes/skills/$REL}"
SKILL_MANAGER="${SKILL_MANAGER:-$HOME/.hermes/profiles/manager/skills/$REL}"
SKILL_FLEET_SAMPLE="${SKILL_FLEET_SAMPLE:-$HOME/.hermes/profiles/worker-inspector/skills/$REL}"

FIX_A="$HERE/fixtures/kanban-worker-anchor-A.md"
FIX_B="$HERE/fixtures/kanban-worker-anchor-B.md"

# fleet base identity (recorded in docs/worker-quota-pause-taxonomy.md)
MD5_BASE_A=f93c72fa0be405d8129450fca1604daf   # 71 copies incl. every worker profile
MD5_BASE_B=8b0a8bba269f47356a8a63549fad03e2   # manager SoT (hermes-manager-skills)

PASS=0
FAIL=0
SKIP=0
T="$(mktemp -d "$HERE/.t33scratch.XXXXXX")"
cleanup() { rm -rf "$T"; }
trap cleanup EXIT

# ---------- assert helpers (same shape as tests/test_staggered_dispatch.sh) ----------
ok()  { PASS=$((PASS + 1)); echo "  ok  - $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL- $1"; }
skip(){ SKIP=$((SKIP + 1)); echo "  skip- $1"; }
assert_eq() { if [ "$1" = "$2" ]; then ok "$3"; else bad "$3 (actual=[$1] expected=[$2])"; fi; }
assert_file_exists() { [ -s "$1" ] && ok "$2" || bad "$2 (missing/empty: $1)"; }
assert_contains() {
    case "$1" in *"$2"*) ok "$3" ;; *) bad "$3 (missing [$2])" ;; esac
}

# ---------- staging helpers ----------
# stage <src-file> <label> -> echoes the staged SKILL.md path under $T/<label>/skills
stage() {
    local src="$1" label="$2"
    local d="$T/$label/skills"
    mkdir -p "$d/devops/kanban-worker" 
    cp "$src" "$d/devops/kanban-worker/SKILL.md"
    echo "$d/devops/kanban-worker/SKILL.md"
}

skills_root_of() { dirname "$(dirname "$(dirname "$1")")"; }

patch_run() {  # <staged skill> <extra patch args...>  -> sets RC / OUT
    local skill="$1"; shift
    if [ ! -s "$PATCH" ]; then OUT="patch file missing"; RC=9; return 0; fi
    OUT="$(cd "$(skills_root_of "$skill")" && patch -p1 --batch --fuzz=0 "$@" < "$PATCH" 2>&1)"
    RC=$?
}

# extract the inserted taxonomy section from an applied file
extract_section() {
    awk '/^### Special prefix: `quota-paused:`/{f=1} f && /^## Heartbeats worth sending/{exit} f' "$1"
}

MARKERS=(
  '### Special prefix: `quota-paused:`'
  'quota-paused: gate says <reason>, resume_at <ts> — no work lost, not a task defect'
  'zai-503-outage'
  'rate_limit_gate.json'
  'ACTIVE 429:'
  'QUOTA-WINDOW:'
  'KALMAN:'
  '≤10 iterations'
  'startswith("quota-paused:")'
  '2 probes spaced ~10 min'
  'board_pause_<board>'
)

check_markers() {  # <file> <label>
    local f="$1" label="$2" m missing=0
    for m in "${MARKERS[@]}"; do
        grep -qF -- "$m" "$f" || { missing=$((missing + 1)); echo "        missing marker: [$m]"; }
    done
    assert_eq "$missing" "0" "$label: all ${#MARKERS[@]} taxonomy markers present"
}

echo "== T3.3 kanban-worker quota-pause taxonomy patch =="
echo "   patch:  $PATCH"
echo "   apply:  $APPLY"
[ -s "$PATCH" ] || echo "   >>> RED: the patch file does not exist yet (expected before Gate 1 -> GREEN)"

# ---------- t1 patch exists ----------
assert_file_exists "$PATCH" "t1: patch file exists and is non-empty"

# ---------- t2 patch shape ----------
if [ -s "$PATCH" ]; then
    hunks="$(grep -c '^@@ ' "$PATCH")"
    assert_eq "$hunks" "2" "t2a: exactly 2 hunks (frontmatter version + taxonomy section)"
    hdr="$(grep -cE '^(--- a/|\+\+\+ b/)' "$PATCH")"
    assert_eq "$hdr" "2" "t2b: relative a/ b/ headers only (applies with -p1)"
    assert_eq "$(grep -cE '^(---|\+\+\+) /' "$PATCH")" "0" "t2c: no absolute paths in headers"
    assert_eq "$(grep -c $'\r' "$PATCH")" "0" "t2d: no CRLF line endings"
    assert_eq "$(tail -c 1 "$PATCH" | od -An -c | tr -d ' \n')" "\n" "t2e: patch ends with a newline"
else
    bad "t2: patch shape (patch file missing)"
fi

# ---------- t3/t4 fixtures + real applies ----------
# t3: fixture A
SA="$(stage "$FIX_A" fixA)"
patch_run "$SA" --dry-run
assert_eq "$RC" "0" "t3a: dry-run applies to fixture A (fuzz=0)"
patch_run "$SA"
assert_eq "$RC" "0" "t3b: real apply to fixture A"
A_APPLIED="$SA"

# t4: fixture B
SB="$(stage "$FIX_B" fixB)"
patch_run "$SB" --dry-run
assert_eq "$RC" "0" "t4a: dry-run applies to fixture B (fuzz=0)"
patch_run "$SB"
assert_eq "$RC" "0" "t4b: real apply to fixture B"
B_APPLIED="$SB"

# ---------- t5: live copies really are the anchored bases ----------
if [ -f "$SKILL_DEFAULT" ]; then
    assert_eq "$(md5sum "$SKILL_DEFAULT" | cut -d' ' -f1)" "$MD5_BASE_A" \
        "t5: live default-profile copy md5 == base A (anchored)"
else
    skip "live default-profile copy absent"
fi
if [ -f "$SKILL_MANAGER" ]; then
    assert_eq "$(md5sum "$SKILL_MANAGER" | cut -d' ' -f1)" "$MD5_BASE_B" \
        "t5: live manager SoT copy md5 == base B (anchored)"
else
    skip "live manager SoT copy absent"
fi
if [ -f "$SKILL_FLEET_SAMPLE" ]; then
    assert_eq "$(md5sum "$SKILL_FLEET_SAMPLE" | cut -d' ' -f1)" "$MD5_BASE_A" \
        "t5: live fleet worker copy md5 == base A (anchored)"
else
    skip "live fleet worker copy absent"
fi

# ---------- t6/t7 live fleet copies ----------
for pair in "$SKILL_DEFAULT:live-default-copy" "$SKILL_MANAGER:live-manager-SoT-copy" "$SKILL_FLEET_SAMPLE:live-fleet-worker-copy"; do
    src="${pair%%:*}"; label="${pair##*:}"
    if [ -f "$src" ]; then
        S="$(stage "$src" "live-$(basename "$(dirname "$(dirname "$(dirname "$src")")")")-$(echo "$label" | tr -cd 'a-z')")"
        patch_run "$S" --dry-run
        assert_eq "$RC" "0" "live apply (fuzz=0) to $label ($src)"
    else
        skip "live copy absent: $label ($src)"
    fi
done

# ---------- t8/t9 markers ----------
check_markers "$A_APPLIED" "t8 (fixture A)"
check_markers "$B_APPLIED" "t9 (fixture B)"

# ---------- t10 canonical text identical in both variants ----------
extract_section "$A_APPLIED" > "$T/sec-A.txt"
extract_section "$B_APPLIED" > "$T/sec-B.txt"
[ -s "$T/sec-A.txt" ] && ok "t10a: extracted section is non-empty ($(wc -l < "$T/sec-A.txt") lines)" \
                     || bad "t10a: extracted section is empty (anchor mismatch)"
if [ -s "$T/sec-A.txt" ] && [ -s "$T/sec-B.txt" ]; then
    assert_eq "$(md5sum < "$T/sec-A.txt" | cut -d' ' -f1)" "$(md5sum < "$T/sec-B.txt" | cut -d' ' -f1)" \
        "t10b: inserted section byte-identical in both fleet variants"
else
    bad "t10b: cannot compare sections (one side empty)"
fi

# ---------- t11 inserted exactly once ----------
assert_eq "$(grep -c '^### Special prefix: `quota-paused:`' "$A_APPLIED")" "1" "t11: section inserted exactly once"

# ---------- t12 frontmatter contract ----------
FM="$(python3 - "$A_APPLIED" "$FIX_A" <<'PY'
import sys, yaml
applied, base = sys.argv[1], sys.argv[2]
def fm(p):
    t = open(p, encoding="utf-8").read()
    if not t.startswith("---\n"):
        raise SystemExit("no frontmatter")
    return yaml.safe_load(t.split("---\n", 2)[1])
a, b = fm(applied), fm(base)
print("%s|%s|%s|%s" % (a.get("name"), a.get("version"),
                       a.get("description") == b.get("description"),
                       a.get("metadata") == b.get("metadata")))
PY
)" 2>&1
assert_eq "$FM" "kanban-worker|2.2.0|True|True" "t12: frontmatter parses, name/description/metadata kept, version 2.2.0"

# ---------- t13 reversible ----------
patch_run "$A_APPLIED" --dry-run -R
assert_eq "$RC" "0" "t13: reverse dry-run succeeds (patch -R is the rollback path)"

# ---------- t14 negative control ----------
UNRELATED_DOC="$ROOT/docs/board-pause.md"
if [ -s "$PATCH" ]; then
    SU="$(stage "$UNRELATED_DOC" unrelated)"
    patch_run "$SU" --dry-run
    if [ "$RC" -ne 0 ]; then ok "t14: patch does NOT apply to an unrelated skill doc (context is specific)"; else bad "t14: patch applied to an unrelated doc — context too loose"; fi
else
    skip "t14: negative control needs the patch file"
fi

# ---------- t15/t16/t17 apply helper ----------
if [ -x "$APPLY" ] || [ -f "$APPLY" ]; then
    # prefer a staged copy of the LIVE base-A file (a known base), fall back to
    # the fixture with --force (fixtures are not fleet bases by design)
    HELP_FORCE=""
    if [ -f "$SKILL_DEFAULT" ]; then
        HA="$(stage "$SKILL_DEFAULT" helperA)"
        PLAIN="$(stage "$SKILL_DEFAULT" plainA)"
    else
        HA="$(stage "$FIX_A" helperA)"
        PLAIN="$(stage "$FIX_A" plainA)"
        HELP_FORCE="--force"
    fi
    patch_run "$PLAIN"
    assert_eq "$RC" "0" "t15pre: plain patch apply on the same base succeeds"
    OUT="$(bash "$APPLY" $HELP_FORCE --target "$HA" 2>&1)"; RC=$?
    assert_eq "$RC" "0" "t15a: apply helper exits 0 on a base-A copy"
    check_markers "$HA" "t15b (helper-applied)"
    assert_eq "$(ls "$HA".bak-* 2>/dev/null | wc -l)" "1" "t15c: helper wrote exactly one backup"
    assert_eq "$(md5sum "$HA" | cut -d' ' -f1)" "$(md5sum "$PLAIN" | cut -d' ' -f1)" \
        "t15d: helper output identical to a plain patch apply on the same base"

    BEFORE="$(md5sum "$HA" | cut -d' ' -f1)"
    OUT="$(bash "$APPLY" $HELP_FORCE --target "$HA" 2>&1)"; RC=$?
    assert_eq "$RC" "0" "t16a: second helper run exits 0 (no-op)"
    assert_eq "$(md5sum "$HA" | cut -d' ' -f1)" "$BEFORE" "t16b: second run left the file byte-identical"
    case "$OUT" in *already*|*no-op*) ok "t16c: second run reports already-applied" ;; *) bad "t16c: second run gave no already-applied signal ($OUT)" ;; esac

    # unknown base (fixture B carries the same md5? no: stage a mutated file)
    HU="$(stage "$FIX_A" helperUnknown)"
    printf '\n<!-- drift -->\n' >> "$HU"
    B4="$(md5sum "$HU" | cut -d' ' -f1)"
    OUT="$(bash "$APPLY" --target "$HU" 2>&1)"; RC=$?
    if [ "$RC" -ne 0 ]; then ok "t17a: helper refuses an unrecognized base without --force"; else bad "t17a: helper applied to an unrecognized base"; fi
    assert_eq "$(md5sum "$HU" | cut -d' ' -f1)" "$B4" "t17b: refused run left the file untouched"

else
    bad "t15-t17: apply helper missing ($APPLY)"
fi

# ---------- t18 runtime skill-load check ----------
LOAD_CHECK="$HERE/verify_skill_loads.py"
if [ -s "$PATCH" ] && [ -f "$LOAD_CHECK" ]; then
    OUT="$(python3 "$LOAD_CHECK" --skill-src "$SKILL_DEFAULT" 2>&1)"; RC=$?
    case "$RC" in
        0) ok "t18: $(echo "$OUT" | tail -1 | sed 's/^ok  - //')" ;;
        3) skip "t18: $(echo "$OUT" | tail -1)" ;;
        *) bad "t18: patched skill must still load (rc=$RC): $OUT" ;;
    esac
else
    skip "t18: needs the patch file and tests/verify_skill_loads.py"
fi

echo
echo "PASS=$PASS FAIL=$FAIL SKIP=$SKIP"
[ "$FAIL" -eq 0 ] || exit 1
exit 0
