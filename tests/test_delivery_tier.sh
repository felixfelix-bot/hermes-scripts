#!/usr/bin/env bash
# D-144: the `delivery` tier, pinned end-to-end in this repo's own suite.
#
# Companion to tests/test_delivery_evidence.py (pytest), for environments that
# run only `tests/*_test.sh`. Both assert the same things:
#
#   1. the spec routes ONLY the explicit `delivery-only` / `evidence-only` tags
#      to the `delivery` tier, and the tier's require list contains nothing
#      structurally unsatisfiable for a card with no code change;
#   2. `delivery_evidence` is satisfied by a PUBLISHED artifact URL;
#   3. prose asserting "delivered" does NOT satisfy it;
#   4. hardening (cold review of PR #10): the predicate is scoped to the card
#      RESULT + post-completion comments, so a card's own instruction surface
#      ("post the evidence to <PR URL>") cannot credit the gate; a bare
#      filename (`http://a.png`) and a placeholder host (`…invalid/x/blob/y`)
#      are not evidence.
#
# (3) is the one that matters most. A predicate that credits prose is a rubber
# stamp, and the card that caused D-144 ("Posted it. Delivered.") would have
# passed — which is precisely the loop this tier exists to stop. It was
# unreachable before the engine change landed: the require list named
# `delivery_evidence` but `evaluate()` had no branch for it, so the card passed
# VACUOUSLY (missing was empty, verdict "pass") while nothing had proved
# publication.
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
URL = 'https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890'
res = 'Posted the evidence as a PR comment ' + URL + '\nsecret-scan: clean'
r = ge.evaluate('delivery', res, 'deepseek/deepseek-flash', gates=SPEC,
                ci_required=False, secrets_hits=[], delivery_text=res)
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
res = 'Delivered the thing to the operator, all done.\nsecret-scan: clean'
r = ge.evaluate('delivery', res, 'deepseek/deepseek-flash', gates=SPEC,
                ci_required=False, secrets_hits=[], delivery_text=res)
sys.exit(0 if (r['verdict'] == 'block' and 'delivery_evidence' in r['missing']) else 1)
" 2>/dev/null; then ok "prose-only delivery card blocks"; else bad "prose-only delivery card blocks"; fi

# the card's own instruction surface must NOT credit (cold review finding 1)
if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
SPEC = json.load(open('$REPO/gates.default.json'))
URL = 'https://github.com/felixfelix-bot/hermes-scripts/pull/1#issuecomment-1234567890'
agg = 'TASK: post the evidence to ' + URL + '\nDone - followed the instructions.\nsecret-scan: clean'
r = ge.evaluate('delivery', agg, 'deepseek/deepseek-flash', gates=SPEC,
                ci_required=False, secrets_hits=[],
                delivery_text='Done - followed the instructions.\nsecret-scan: clean')
sys.exit(0 if (r['verdict'] == 'block' and 'delivery_evidence' in r['missing']) else 1)
" 2>/dev/null; then ok "the target URL in the card's instructions does not credit"; else bad "the target URL in the card's instructions does not credit"; fi

# fail closed: an unscoped call must never pass the delivery tier
if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
SPEC = json.load(open('$REPO/gates.default.json'))
agg = 'posted https://github.com/felixfelix-bot/hermes-scripts/pull/1\nsecret-scan: clean'
r = ge.evaluate('delivery', agg, 'deepseek/deepseek-flash', gates=SPEC,
                ci_required=False, secrets_hits=[])
sys.exit(0 if r['verdict'] == 'block' else 1)
" 2>/dev/null; then ok "delivery tier fails closed without a scoped surface"; else bad "delivery tier fails closed without a scoped surface"; fi

# the spec half: only the explicit -only tags route
if PYTHONPATH="$REPO" python3 -c "
import json, sys
import gate_engine as ge
d = json.load(open('$REPO/gates.default.json'))
t = d['tiers'].get('delivery') or {}
tags = d['tag_tiers']
known = ge.known_tags(d)
sys.exit(0 if (
    t.get('require') == ['delivery_evidence', 'secrets_clean']
    and t.get('enforce') == 'block'
    and all(tags.get(x) == 'delivery' for x in ('delivery-only', 'evidence-only'))
    and not ({'delivery', 'evidence'} & set(tags))
    and not ({'delivery', 'evidence'} & known)
    and ge.classify_tier('b', ['delivery'], d) == 'code'
    and ge.classify_tier('b', ['evidence'], d) == 'code'
    and ge.classify_tier('b', ['delivery-only'], d) == 'delivery'
) else 1)
" 2>/dev/null; then ok "spec routes only the explicit -only tags (generic words fall back to code)"; else bad "spec routes only the explicit -only tags (generic words fall back to code)"; fi

# the predicate's negative half: prose, bare filenames, placeholder hosts
if PYTHONPATH="$REPO" python3 -c "
import sys
import gate_engine as ge
bad = ['Delivered the thing to the operator, all done.', 'Done. Posted it.',
       'The artifact has been published.',
       'http://a.png', 'see the video at deadbeef.webm',
       'https://not-a-real-host.invalid/x/blob/y',
       'https://example.org/shot.png',
       'https://github.com/o/r/pull/%s']
sys.exit(0 if not any(ge.delivery_evidence_present(p) for p in bad) else 1)
" 2>/dev/null; then ok "delivery_evidence_present rejects prose, bare filenames and placeholder hosts"; else bad "delivery_evidence_present rejects prose, bare filenames and placeholder hosts"; fi

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
