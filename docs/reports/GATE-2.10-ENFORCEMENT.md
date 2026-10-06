# Gate 2.10 — PCB review/consult enforcement (GATE-2.10-ENFORCEMENT.md)

**Status: ENFORCED, LIVE, VERIFIED, PUSHED.** 2026-10-07.

## What the gate does

A card whose first `tags:` line contains any of `pcb|schematic|board|kicad|layout|fab|gerber`
must carry BOTH of these evidence lines, taken from the result / comments at or after the
card's `completed_at` mark (never from the card body):

```
pcb_review: verdict=<APPROVED|CHANGES_REQUESTED> reviewer_model=<concrete id> \
            reviewer_profile=pcb-reviewer artifact=<path|URL> drc=<int> erc=<int> \
            netlist_parity=<ok|diff:n>

pcb_consult: verdict=<APPROVED|CHANGES_REQUESTED> reviewer_model=<concrete id> \
             reviewer_profile=pcb-consultant artifact=<path|URL>
```

Enforced rules (all fail CLOSED, naming the missing/invalid field):

| Rule | Behaviour |
|---|---|
| verdict | exactly `APPROVED` or `CHANGES_REQUESTED` |
| reviewer_profile | `pcb-reviewer` / `pcb-consultant` respectively |
| reviewer_model | a CONCRETE served id; any `tier/*` alias is rejected |
| artifact | real path (exists, >= 100 bytes) or URL |
| drc / erc | integers; `0` valid; `-`, `?`, `n/a`, `TBD`, empty invalid |
| netlist_parity | `ok` or `diff:<int>` |
| cross-family | reviewer family must differ from the author's |
| cold consult | consultant family must differ from BOTH the reviewer's and the author's |
| evidence source | result/comments at/after completion only — body text cannot self-satisfy |

**Tag-conditional → zero fleet blast radius.** `pcb_review`/`pcb_consult` are declared in the
`code` tier's `require` list, but the predicates are only evaluated when the tag trigger
matches. An untagged code card demands neither (asserted in the suite and in the E2E).

## Verification evidence

| Check | Result |
|---|---|
| Live engine spec (`~/.hermes/bot/gates.json`) declares both gates | yes (backup `gates.json.bak-20261006T235528`) |
| Engine unit suite `tests/test_gate_engine.py` | **27 tests, OK** |
| Independent E2E harness (`gate210_e2e.py`, scratch kanban DB, real `evaluate_task`) | **ALL GREEN, exit 0** |
| Untagged card regression | not required (`tagged=False`, pcb gates absent from `missing`) |
| Blocked verdict JSON | `reports/balloon-board-state/gate210_blocked.json` |
| Passing verdict JSON | `reports/balloon-board-state/gate210_passing.json` |

E2E fixtures: `t_plain` (untagged), `t_pcb_empty` (tagged, no evidence → blocks on BOTH),
`t_pcb_ok`, `t_pcb_alias` (rejects `reviewer_model=tier/schematic-review`),
`t_pcb_noart` (missing artifact).

## The merge defect this document exists to record

The first attempt carried the **deployed** engine file to the branch. That file *predates*
master's delivery-evidence hardening, so the branch diff was `+313 / −156`: merging it would
have **deleted 142 lines of another agent's committed work** (`RE_DELIVERY_URL`,
`RE_PLACEHOLDER_HOST`, `RE_TEMPLATED_URL`, `RE_RAW_CONTENT_HOST`). Never merge that commit.

Rebuilt against master itself by content anchor — master's engine plus only the pcb pieces
(helpers block, `evaluate()` hook, signature params, `evaluate_task()` wiring, call args):

- `reports/balloon-consolidation/merge_gate210.py` — the splice tool, with assertions
  (`once()` requires an anchor to match exactly once; hard-fails otherwise).
- Result: `git diff --numstat origin/master` → `gate_engine.py +308/−0`,
  `tests/test_gate_engine.py +371/−0`, `gates.default.json +6/−4`.
  **Zero deletions of master's lines.**

Lesson: when the deployed file and the committed file have drifted, never publish the
deployed file. Diff it against the branch point first; if deletions appear, rebuild.

## State

- Branch `pr/gate210-pcb-predicates` (repo `hermes-scripts`), PR #13.
- Commits: `6d86d22` (initial, superseded) → **`b45fe74`** (correct rebase; remote head == local).
- Live engine installed from the merged file; backup `~/.hermes/scripts/gate_engine.py.bak-20261007T010448`.
- Live engine contains BOTH feature sets: `RE_DELIVERY_URL`, `RE_PLACEHOLDER_HOST`,
  `pcb_review_present`, `pcb_consult_present`, `pcb_tagged`.

## Not done / caveats

1. The shared tree `~/.hermes/scripts` had another agent's **staged deletions** (a dozen
   scripts) — untouched by this work; staging was by explicit path only.
2. Full suite: 96 tests, 1 error = `ModuleNotFoundError: No module named 'coincurve'`
   (`test_nosigner_nip04`) — pre-existing environment gap, unrelated.
3. PR #13 was opened from an unsound commit; its history still contains `6d86d22`.
   The tip is correct; the operator may prefer to squash on merge.
