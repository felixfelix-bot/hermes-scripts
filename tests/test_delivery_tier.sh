#!/usr/bin/env bash
# D-144: the `delivery` tier, pinned end-to-end in this repo's own suite.
#
# Companion to tests/test_delivery_evidence.py (pytest), for environments that
# run only `tests/*_test.sh`. Both assert the same three things:
#
#   1. the spec routes the delivery tags and the tier's require list contains
#      nothing structurally unsatisfiable for a card with no code change;
#   2. `delivery_evidence` is satisfied by a PUBLISHED artifact URL;
#   3. prose asserting "delivered" does NOT satisfy it.
#
# (3) is the one that matters. A predicate that credits prose is a rubber stamp,
# and the card that caused D-144 ("Posted it. Delivered.") would have passed —
# which is precisely the loop this tier exists to stop. It was unreachable
# before the engine change landed: the require list named `delivery_evidence`
# but `evaluate()` had no branch for it, so the card passed VACUOUSLY (missing
# was empty, verdict "pass") while nothing had proved publication.
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
pass=0; fail=0
ok()  { echo "ok   - $1"; pass=$((pass+1)); }
bad() { echo "FAIL - $1"; fail=$((fail+1)); }

probe() { # probe <label> <python-expression-on-r>
  local label="$1" expr="$2"
  if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
SPEC = json.load(open('$REPO/gates.default.json'))
r = ge.evaluate('delivery',
                'Posted the evidence as a PR comment '
                'https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890\n'
                'secret-scan: clean',
                'deepseek/deepseek-flash', gates=SPEC, ci_required=False,
                secrets_hits=[])
sys.exit(0 if ($expr) else 1)
" 2>/dev/null; then ok "$label"; else bad "$label"; fi
}

probe "delivery card with a published URL passes"      "r['verdict'] == 'pass'"
probe "delivery_evidence is in passed (not vacuous)"   "'delivery_evidence' in r['passed']"
probe "delivery tier does not require ci_evidence"     "'ci_evidence' not in r['missing']"
probe "delivery tier does not require tests_green"     "'tests_green' not in r['missing']"
probe "delivery tier does not require consolidated"    "'consolidated' not in r['missing']"

# prose must NOT pass
if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
SPEC = json.load(open('$REPO/gates.default.json'))
r = ge.evaluate('delivery',
                'Delivered the thing to the operator, all done.\nsecret-scan: clean',
                'deepseek/deepseek-flash', gates=SPEC, ci_required=False, secrets_hits=[])
sys.exit(0 if (r['verdict'] == 'block' and 'delivery_evidence' in r['missing']) else 1)
" 2>/dev/null; then ok "prose-only delivery card blocks"; else bad "prose-only delivery card blocks"; fi

# the spec half
if PYTHONPATH="$REPO" python3 -c "
import json, sys
d = json.load(open('$REPO/gates.default.json'))
t = d['tiers'].get('delivery') or {}
tags = d['tag_tiers']
sys.exit(0 if (
    t.get('require') == ['delivery_evidence', 'secrets_clean']
    and t.get('enforce') == 'block'
    and all(tags.get(x) == 'delivery' for x in
            ('delivery-only', 'delivery', 'evidence-only', 'evidence'))
) else 1)
" 2>/dev/null; then ok "spec defines the tier and routes delivery tags"; else bad "spec defines the tier and routes delivery tags"; fi

# the predicate's negative half: prose is never a URL
if PYTHONPATH="$REPO" python3 -c "
import sys
import gate_engine as ge
bad = ['Delivered the thing to the operator, all done.', 'Done. Posted it.',
       'The artifact has been published.']
sys.exit(0 if not any(ge.RE_DELIVERED_URL.search(p) for p in bad) else 1)
" 2>/dev/null; then ok "RE_DELIVERED_URL rejects prose"; else bad "RE_DELIVERED_URL rejects prose"; fi

# the premise: without the tier, a delivery-only card is unsatisfiable (code tier)
if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
SPEC = json.load(open('$REPO/gates.default.json'))
r = ge.evaluate('code', 'https://github.com/felixfelix-bot/hermes-scripts/pull/1\nsecret-scan: clean',
                'deepseek/deepseek-flash', gates=SPEC, ci_required=True, secrets_hits=[])
sys.exit(0 if (r['verdict'] == 'block' and
               set(r['missing']) & {'ci_evidence', 'tests_green', 'consolidated'}) else 1)
" 2>/dev/null; then ok "code tier on a delivery-only card is unsatisfiable (the D-144 premise)"; else bad "code tier on a delivery-only card is unsatisfiable (the D-144 premise)"; fi

echo
echo "delivery_tier_test: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
