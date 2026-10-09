
## Hold semantics

`fleet_arbiter.py` emits remediation intents with a `target` node. The scheduler MUST keep this scope:
a hold for node A prevents A from advertising/claiming work, but MUST NOT deny work to node B.
`hold` in `fleet_boards.json` is retained only as a local emergency switch for this node; it is
not a fleet-wide brake. Peer remediation is represented by the per-node intent/lease channel.

The regression test `tests/test_fleet_hold_scope.py` protects the target-node contract.
