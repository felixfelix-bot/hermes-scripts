#!/bin/bash
# apply-kanban-worker_quota-pause-taxonomy.sh — T3.3 (decision D9=B).
#
# Applies patches/kanban-worker_quota-pause-taxonomy.patch to kanban-worker
# SKILL.md copies. The worker drafts the patch; the MANAGER runs this and
# commits the result (see docs/worker-quota-pause-taxonomy.md).
#
# Usage
#   bash apply-kanban-worker_quota-pause-taxonomy.sh                  # canonical live targets that exist
#   bash apply-kanban-worker_quota-pause-taxonomy.sh --target <file>  # repeatable
#   bash apply-kanban-worker_quota-pause-taxonomy.sh --dry-run        # report only, no writes
#   bash apply-kanban-worker_quota-pause-taxonomy.sh --force          # allow an unknown base md5
#   bash apply-kanban-worker_quota-pause-taxonomy.sh --patch <file>   # alternate patch file
#
# Safety properties
#   * base preflight: the target's md5 must be a known fleet base
#     (base A = default/worker variant, base B = manager SoT variant) unless
#     --force is given — a divergent copy needs the patch re-anchored, not applied;
#   * timestamped .bak written before any write;
#   * applied with --fuzz=0 (context must match exactly, offsets are fine);
#   * post-apply marker verification; on failure the backup is restored;
#   * a partially-applied file (some but not all markers) is REFUSED as an
#     unknown state — restore the .bak-* backup or re-anchor, never guess;
#   * a partial apply's `.rej` is removed so no litter is left in the tree;
#   * re-running on an already-patched file is a no-op (idempotent).
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="${HERE}/kanban-worker_quota-pause-taxonomy.patch"

# fleet bases this patch was anchored against (see docs/worker-quota-pause-taxonomy.md)
MD5_BASE_A=f93c72fa0be405d8129450fca1604daf   # default-profile copy + 70 worker-profile copies
MD5_BASE_B=8b0a8bba269f47356a8a63549fad03e2   # manager SoT (hermes-manager-skills)

MARKER_HEAD='### Special prefix: `quota-paused:`'
MARKER_REASON='quota-paused: gate says <reason>'
MARKER_CONTRACT='startswith("quota-paused:")'

FORCE=0
DRY_RUN=0
TARGETS=()

while [ "$#" -gt 0 ]; do
    case "$1" in
        --target) TARGETS+=("${2:?--target needs a path}"); shift 2 ;;
        --force) FORCE=1; shift ;;
        --dry-run) DRY_RUN=1; shift ;;
        --patch) PATCH="${2:?--patch needs a path}"; shift 2 ;;
        -h|--help) sed -n '2,29p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [ "${#TARGETS[@]}" -eq 0 ]; then
    # canonical live targets; missing ones are skipped (other profiles/hosts)
    TARGETS=(
        "$HOME/.hermes/skills/devops/kanban-worker/SKILL.md"
        "$HOME/.hermes/profiles/manager/skills/devops/kanban-worker/SKILL.md"
    )
fi

if [ ! -s "$PATCH" ]; then
    echo "ERROR: patch file not found or empty: $PATCH" >&2
    exit 2
fi

md5of() { md5sum "$1" | cut -d' ' -f1; }
skills_root_of() { dirname "$(dirname "$(dirname "$1")")"; }

RC=0
for TARGET in "${TARGETS[@]}"; do
    echo "== $TARGET"
    if [ ! -f "$TARGET" ]; then
        echo "   skip: not present on this host"
        continue
    fi
    BEFORE="$(md5of "$TARGET")"

    # idempotency: require ALL markers. A file with only some of them is an
    # unknown partial state (interrupted apply / hand edit), NOT "already done".
    present=0
    for m in "$MARKER_HEAD" "$MARKER_REASON" "$MARKER_CONTRACT"; do
        grep -qF -- "$m" "$TARGET" && present=$((present + 1))
    done
    if [ "$present" -eq 3 ]; then
        echo "   no-op: taxonomy already applied (md5 $BEFORE)"
        continue
    fi
    if [ "$present" -ne 0 ]; then
        echo "   REFUSED: partial state ($present/3 markers present) — neither a known"
        echo "            base nor a fully patched file. Restore the .bak-* backup, or"
        echo "            re-anchor; this script will not guess."
        RC=1
        continue
    fi

    case " $MD5_BASE_A $MD5_BASE_B " in
        *" $BEFORE "*)
            echo "   base: $BEFORE (known fleet base)" ;;
        *)
            echo "   base: $BEFORE (UNKNOWN — not a base this patch was anchored against)"
            if [ "$FORCE" -ne 1 ]; then
                echo "   REFUSED: re-anchor the patch for this variant, or pass --force if you verified it"
                RC=1
                continue
            fi
            echo "   --force given: applying anyway" ;;
    esac

    if [ "$DRY_RUN" -eq 1 ]; then
        OUT="$(cd "$(skills_root_of "$TARGET")" && patch -p1 --batch --fuzz=0 --dry-run < "$PATCH" 2>&1)"; PRC=$?
        echo "$OUT" | sed 's/^/   /'
        if [ "$PRC" -eq 0 ]; then echo "   dry-run OK (--fuzz=0)"; else echo "   dry-run FAILED (rc=$PRC)"; RC=1; fi
        continue
    fi

    BAK="${TARGET}.bak-$(date +%s)"
    cp -p "$TARGET" "$BAK"
    OUT="$(cd "$(skills_root_of "$TARGET")" && patch -p1 --batch --fuzz=0 < "$PATCH" 2>&1)"; PRC=$?
    echo "$OUT" | sed 's/^/   /'

    # a partially-applied file leaves a .rej beside the target: never leave
    # litter (reject files) in an operator skills tree
    REJ="${TARGET}.rej"
    if [ -e "$REJ" ]; then
        rm -f "$REJ"
        echo "   removed stray reject file: $REJ"
    fi

    okmark=1
    if [ "$PRC" -ne 0 ]; then okmark=0; echo "   patch failed (rc=$PRC)"; fi
    for m in "$MARKER_HEAD" "$MARKER_REASON" "$MARKER_CONTRACT"; do
        grep -qF -- "$m" "$TARGET" || { okmark=0; echo "   marker missing after apply: [$m]"; }
    done

    if [ "$okmark" -eq 1 ]; then
        echo "   applied: md5 $BEFORE -> $(md5of "$TARGET")  (backup: $BAK)"
    else
        cp -p "$BAK" "$TARGET"
        echo "   RESTORED from backup ($BAK); target left byte-identical to $BEFORE"
        RC=1
    fi
done

if [ "$DRY_RUN" -eq 1 ]; then
    exit "$RC"
fi
echo "done (rc=$RC). Review the diff and commit in the owning repo — this script never commits."
exit "$RC"
