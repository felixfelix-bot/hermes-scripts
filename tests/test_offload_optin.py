"""Config-as-code drift test for the private fleet offload opt-in.

The tracked config/offload_boards.json is the source of truth for extra boards
this operator opts into the PRIVATE offload ledger. This test asserts the live
map the scheduler reads matches it, and that the scheduler then classifies those
boards as "private" (a peer may claim the card) rather than node-local (never
advertised, so the card can only ever run on this box).
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fleet_scheduler as fs  # noqa: E402

CFG = json.loads((ROOT / "config" / "offload_boards.json").read_text())["private_offload"]
MAP = Path.home() / ".hermes" / "bot" / "fleet_map" / "private_offload_boards.json"


def _live() -> dict:
    if not MAP.exists():
        return {}
    try:
        d = json.loads(MAP.read_text())
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def test_tracked_boards_are_deployed():
    live = _live()
    missing = [b for b in CFG if b not in live]
    assert not missing, (
        f"tracked in config/offload_boards.json but not deployed: {missing} "
        "-> run install-offload-optin.sh")


def test_deployed_repo_matches_tracked():
    live = _live()
    for board, meta in CFG.items():
        if board in live:
            assert live[board].get("repo") == meta.get("repo"), board


def test_opted_in_board_is_private_not_node_local():
    live = _live()
    public = fs._load_map("public_boards.json")
    local = fs._load_map("local_only_boards.json")
    for board in CFG:
        got = fs._advertise_transport(board, public, live, local)
        assert got == "private", f"{board} classifies as {got!r}, expected 'private'"
