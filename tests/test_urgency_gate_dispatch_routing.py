"""Regression (live 2026-09-17): `hermes kanban dispatch` routed through the
~/.local/bin/hermes shim -> urgency_gate.py gate dropped the 'kanban' token,
so the real CLI received `--board X dispatch` at top level and argparse died
with "invalid choice" for the board name or 'dispatch'. The shim shimmed
creates fine (run_real prepends 'kanban') but every dispatch was silently
broken."""
import importlib.util
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("urgency_gate", HERE / "urgency_gate.py")
assert spec is not None and spec.loader is not None
ug = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ug)


def test_gate_dispatch_exec_preserves_kanban_token(monkeypatch):
    calls = []
    monkeypatch.setattr(ug.os, "execv", lambda exe, argv: calls.append(argv))
    monkeypatch.setattr(ug, "price_tier", lambda: {"tier": "expensive", "evidence": []})
    monkeypatch.setattr(ug, "sql", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv",
        ["urgency_gate.py", "gate", "kanban", "--board", "plebeian", "dispatch", "--max", "4"],
    )
    ug.main()
    assert calls, "dispatch gate must hand off to the real hermes binary via execv"
    argv = calls[0]
    assert argv[1] == "kanban", f"'kanban' token dropped from real-CLI argv: {argv}"
    assert "dispatch" in argv, f"dispatch verb lost: {argv}"
    assert argv.index("--board") + 1 == argv.index("plebeian")


def test_gate_dispatch_board_first_ordering(monkeypatch):
    """Same bug, observed form: shim is invoked as `gate kanban --board X dispatch`.
    Assert the exec'd argv round-trips that exact ordering back to the real CLI."""
    calls = []
    monkeypatch.setattr(ug.os, "execv", lambda exe, argv: calls.append(argv))
    monkeypatch.setattr(ug, "price_tier", lambda: {"tier": "cheap", "evidence": []})
    monkeypatch.setattr(ug, "sql", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv", ["urgency_gate.py", "gate", "kanban", "dispatch", "--board", "plebeian"]
    )
    ug.main()
    assert calls
    argv = calls[0]
    assert argv[:3] == [ug.REAL, "kanban", "dispatch"], argv
