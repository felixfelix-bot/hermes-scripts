"""Config-as-code drift test for the private fleet offload opt-in.

Two layers, and the distinction matters (learned 2026-10-09):
  * ~/.hermes/bot/board_repos.json           - UPSTREAM mapping (board -> repo)
  * ~/.hermes/bot/fleet_map/private_offload_boards.json - DERIVED opt-in map
The derived map is rebuilt by the classifier on every cycle, so anything written
only there silently disappears and the board's cards stop being advertised to
peers. These tests therefore assert the UPSTREAM mapping, not just the artifact.
"""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import fleet_scheduler as fs

CFG = json.loads((ROOT / "config" / "offload_boards.json").read_text())["private_offload"]
BOT = Path.home() / ".hermes" / "bot"
MAP = BOT / "fleet_map"


def _load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


def test_tracked_boards_are_in_upstream_mapping():
    """The durable layer. If this fails, the next classify cycle wipes the opt-in."""
    repos = _load(BOT / "board_repos.json", {})
    missing = [b for b in CFG if not repos.get(b)]
    assert not missing, (
        f"tracked in config/offload_boards.json but absent from board_repos.json: {missing} "
        f"-> run install-offload-optin.sh (the derived opt-in map is rebuilt from this file)")


def test_tracked_boards_are_in_derived_optin():
    opt = _load(MAP / "private_offload_boards.json", {})
    missing = [b for b in CFG if b not in opt]
    assert not missing, f"not deployed into derived opt-in: {missing} -> run install-offload-optin.sh"


def test_opted_in_board_classifies_private_not_node_local():
    opt = _load(MAP / "private_offload_boards.json", {})
    for b in CFG:
        got = fs._advertise_transport(b, _load(MAP / "public_boards.json", None), opt,
                                      _load(MAP / "local_only_boards.json", None))
        assert got == "private", f"{b} classifies as {got!r}, expected 'private' (node-local = never advertised)"
