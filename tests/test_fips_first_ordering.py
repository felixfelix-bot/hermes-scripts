"""FIPS-first peer address ordering (operator decision 2026-10-09).

The mesh address must outrank mDNS/LAN so peers keep finding each other when
not physically co-located. _order_hosts must put fd* before 192.168.* and
*.local; FLEET_LINK_RANK env can restore the old order.
"""
import importlib, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import fleet_heartbeat as h


def test_fips_first_by_default():
    ordered = h._order_hosts([
        "cobradorwave.local", "192.168.2.43", "192.168.2.33",
        "fd97:77d4:cd27:a6ae:1b29:e92e:fd96:dee8", "100.90.101.9",
    ])
    assert ordered[0].startswith("fd97"), ordered
    assert ordered[-1] == "100.90.101.9"  # netbird last


def test_lan_still_second_class():
    ordered = h._order_hosts(["192.168.2.43", "cobradorwave.local"])
    assert ordered == ["192.168.2.43", "cobradorwave.local"]


def test_env_override_restores_lan_first():
    os.environ["FLEET_LINK_RANK"] = "lan,fips,netbird,other"
    try:
        importlib.reload(h)
        ordered = h._order_hosts(["192.168.2.43", "fd97::1"])
        assert ordered[0] == "192.168.2.43", ordered
    finally:
        del os.environ["FLEET_LINK_RANK"]
        importlib.reload(h)
