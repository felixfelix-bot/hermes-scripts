"""Structural tests for the fleet timer units we ship as config-as-code.

The fleet's systemd user units were hand-installed on cobrador and existed nowhere
in the repo - so a peer node had no reproducible way to get them, and hermes-nvme
sat with a stale fleet_health.json and an invisible fit profile. These tests pin
the units into version control and check they are coherent.
"""
import configparser, subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNITS = ROOT / "systemd" / "user"
PAIRS = ["fleet-heartbeat", "fleet-resource-collector", "fleet-arbiter"]


def test_unit_pairs_exist():
    for name in PAIRS:
        assert (UNITS / f"{name}.service").is_file(), name
        assert (UNITS / f"{name}.timer").is_file(), name


def test_services_have_exec_and_are_oneshot():
    for name in PAIRS:
        c = configparser.ConfigParser(strict=False, interpolation=None)
        c.read(UNITS / f"{name}.service")
        assert c["Service"]["ExecStart"].strip(), name
        assert c["Service"]["Type"].strip() == "oneshot", name


def test_timers_repeat_and_are_installable():
    for name in PAIRS:
        c = configparser.ConfigParser(strict=False, interpolation=None)
        c.read(UNITS / f"{name}.timer")
        assert c["Timer"]["OnUnitActiveSec"].strip(), name
        assert "timers.target" in c["Install"]["WantedBy"], name


def test_installer_is_executable_and_parses():
    s = ROOT / "install-fleet-timers.sh"
    assert s.is_file()
    assert s.stat().st_mode & 0o111, "installer must be executable"
    r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
