# The `delivery` tier and `delivery_evidence`

Status: hardened 2026-10-06 (cold cross-family review of PR #10, merged `2ac12fd`).

## Why the tier exists (D-144)

Some cards do not ship code. Their entire deliverable is "publish this artifact":
post a review to a PR, hand a document to the operator, drop a file somewhere a
human can open.

The `code` tier asks those cards for `tests_green`, `ci_evidence` and
`consolidated`. For a card with no commit, no workflow run and nothing to merge,
those gates are **structurally unsatisfiable** — not "not yet met", impossible.

That is not cosmetic. An unsatisfiable gate on a `done` card makes `gate_tick`
block it; `done` is dispatchable, so the dispatcher re-ran the card, the worker
finished it `done` again, and the next tick blocked it again — every 10 minutes,
a full worker session per pass, on a delivered artifact. `consecutive_failures`
stayed `0` throughout, because every run *succeeded*: the failure circuit breaker
could not see the loop by construction.

## Tier shape

```json
"delivery": {
  "enforce": "block",
  "coverage_threshold": 0,
  "require": ["delivery_evidence", "secrets_clean"]
}
```

`secrets_clean` stays: a card that publishes something must not publish a secret.

Only the **explicit** tags route here:

| tag | tier |
|---|---|
| `delivery-only` | `delivery` |
| `evidence-only` | `delivery` |

`delivery` and `evidence` deliberately do **not** route. They are ordinary
English words that appear in unrelated card text ("evidence: the test passed"),
and a single line of body text must not be able to skip the code tier's tests,
CI and consolidation gates. An untiered card falls back to `code` — the
fail-safe default for work of unknown type (D-165).

## What `delivery_evidence` accepts

A URL that proves something was **published**, evaluated only against the
surfaces a card cannot pre-fill.

Accepted path shapes, on a host that could resolve:

- `…/pull/<n>` (PR, review, review comment, discussion — the fragment rides along)
- `…/releases/…`
- `…/blob/…`, `…/commit/<sha>`
- `…/issues/<n>`, `…/discussions/<n>`
- a raw artifact URL ending in `.mp4 .webm .mov .mkv .png .jpg .jpeg .webp`
- `raw.githubusercontent.com/…` / `raw.github.com/…` with a real file path

Rejected:

- prose ("Delivered the thing, all done") — always was, still is;
- a **failure** report — `could not post to …/pull/12` names the same URL as a
  success and is rejected by the *scoping* rule below, not by the URL shape;
- a bare filename with no host (`http://a.png`, `see shot.png`);
- a host that cannot resolve — `…invalid`, `example.*`, `localhost`, a
  special-use TLD, or a host whose last label is a file extension
  (`https://a.png/x.mp4` — the extension-as-TLD is the tell);
- a templated URL (`/pull/%s`, `/pull/<N>`, `/pull/${id}`) — that is a documented
  *shape*, not a link.

**Residual limit, stated not implied:** a hand-typed bogus-but-plausible host
(`https://acme-notreal.com/x.mp4`) is not detectable without a DNS/HTTP lookup.
The gate is regex-only on purpose — it runs every tick for ~190 boards and must
never make a network call. What the hardening closes is the two classes the
review falsified: filenames with no host, and hosts that cannot resolve.

## Scoping — the load-bearing half

`delivery_evidence` reads **only**:

1. the task **result** (the completion message), and
2. comments whose `created_at` is **after** the task's `completed_at`.

It never reads the aggregate evidence blob, and never the card body.

This matters because the first shipped version *did* read the aggregate blob
(result + the last 30 comments). The card that motivated D-144 said, in its own
comments, "post the evidence to `<PR URL>`". The gate was therefore credited by
the sentence telling the worker what to do — false credit on exactly the card
class the tier was built for. A pre-completion comment is instruction surface or
work-in-progress; the result and any post-completion comment are the claim that
the artifact exists.

`evaluate()` **fails closed**: a caller that passes no scoped text gets
`delivery_evidence` in `missing`, never a pass. `evaluate_task()` always computes
the scoped text via `delivery_evidence_text()`.

## Files

- `gate_engine.py` — `RE_DELIVERY_URL`, `RE_DELIVERY_PATH`, `RE_PLACEHOLDER_HOST`,
  `RE_TEMPLATED_URL`, `RE_RAW_CONTENT_HOST`, `delivery_evidence_present()`,
  `delivery_evidence_text()`, the `evaluate()` branch.
- `gates.default.json` — the tier and its tag routing.
- `tests/test_delivery_evidence.py` (pytest) and `tests/test_delivery_tier.sh`
  (shell-only environments) — both assert the tier routing, the URL shape and
  the scoping, including a real board DB driven through `evaluate_task`.

## Deployment note

`gate_tick` resolves its engine by `sys.path.insert(0, HERE)` from
`~/.hermes/bot/governance/`, so the live cron path imports
`~/.hermes/bot/governance/gate_engine.py` — **not** `~/.hermes/scripts/gate_engine.py`.
Those two copies have diverged. Verify which one the tick actually imports before
claiming a gate change is live:

```bash
python3 -c "import sys; sys.path.insert(0,'$HOME/.hermes/bot/governance'); \
import gate_engine as ge; print(ge.__file__, hasattr(ge,'delivery_evidence_present'))"
```
