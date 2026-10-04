#!/bin/bash
# pre13 release watcher — silent until the release is published OR the build fails.
# Fires exactly once (marker file), then stays silent forever.
MARK="$HOME/.hermes/state/pre13-watch.done"
mkdir -p "$(dirname "$MARK")"
[ -f "$MARK" ] && { echo ""; exit 0; }

TAG="v0.6.0-alpha2-pre13"
REPO="FreedomTechFeed/packages"
RUN="35614641585"
RUN_URL="https://github.com/$REPO/actions/runs/$RUN"

# 1) Release published with real assets?
if gh api "repos/$REPO/releases/tags/$TAG" >/dev/null 2>&1; then
  N=$(gh api "repos/$REPO/releases/tags/$TAG" \
        --jq '[.assets[]? | select(.name | test("ipk$"))] | length' 2>/dev/null)
  case "$N" in ''|*[!0-9]*) N=0 ;; esac
  if [ "$N" -ge 10 ]; then
    echo "pre13 READY — $TAG published ($N ipk assets)."
    echo ""
    echo "Re-test on the MT3000 (newest-alpha now resolves to pre13, no flag needed):"
    echo "curl -fsSL https://raw.githubusercontent.com/OpenTollGate/tollgate-installer/main/install-and-test.sh | bash"
    echo ""
    echo "Or pin it explicitly:"
    echo "curl -fsSL https://raw.githubusercontent.com/OpenTollGate/tollgate-installer/main/install-and-test.sh | bash -s -- --tag $TAG"
    touch "$MARK"
    exit 0
  fi
fi

# 2) Build failed / cancelled?
C=$(gh run view "$RUN" -R "$REPO" --json status,conclusion \
     --jq '.status + "/" + (.conclusion // "none")' 2>/dev/null)
case "$C" in
  completed/failure|completed/cancelled|completed/timed_out|completed/startup_failure)
    echo "pre13 release build did NOT complete cleanly: $C"
    echo "$RUN_URL"
    touch "$MARK"
    exit 0
    ;;
esac

# Still building — stay silent.
echo ""
exit 0
