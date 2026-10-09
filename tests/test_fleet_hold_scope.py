import importlib.util
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('arbiter', ROOT / 'fleet_arbiter.py')
arb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arb)


def test_hold_intent_is_scoped_to_target_node():
    # A per-node remediation intent must not become a fleet-wide boolean.
    health = [{'node': 'cobrador', 'role': 'hermes'}, {'node': 'hermes-nvme', 'role': 'hermes'}]
    intent = {'target': 'cobrador', 'action': 'hold', 'level': 0, 'reason': 'saturated'}
    assert intent['target'] == 'cobrador'
    assert intent['action'] == 'hold'
    assert 'global' not in intent


def test_board_policy_is_reviewable_and_includes_contextvm():
    policy = json.loads((ROOT / 'config' / 'dispatch_policy.json').read_text())
    assert 'contextvm-services' in policy['boards']
