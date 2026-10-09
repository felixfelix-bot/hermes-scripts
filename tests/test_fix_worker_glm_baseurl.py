"""fix-worker-glm-baseurl.sh pins worker GLM traffic to the node's router.

Regression tests for the 2026-10-09 x280 incident (workers with GLM_API_KEY but
no GLM_BASE_URL called api.z.ai directly: 401 + invisible to Kalman filters),
and for the router-less-node hazard the fleet consultant flagged (hermes-nvme
has no local 9099 listener - pinning it to localhost would break it).
"""
import subprocess, tempfile, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "fix-worker-glm-baseurl.sh"


def _run(home, *args):
    env = dict(os.environ, HERMES_HOME=str(home))
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env)


def test_script_is_executable_and_parses():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_missing_baseurl_gets_pinned():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td, "profiles/worker-x/.env")
        p.parent.mkdir(parents=True)
        p.write_text("GLM_API_KEY=somekey\n")
        r = _run(td, "--force")
        assert "fixed:" in r.stdout
        assert "GLM_BASE_URL=http://127.0.0.1:9099" in p.read_text()


def test_router_less_node_is_refused_without_force():
    """No listener on the configured router => refuse, leave .env untouched."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td, "profiles/worker-x/.env")
        p.parent.mkdir(parents=True)
        p.write_text("GLM_API_KEY=somekey\n")
        env = dict(os.environ, HERMES_HOME=td, FLEET_ROUTER_URL="http://127.0.0.1:9")
        r = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True, env=env)
        assert "refusing to pin" in r.stdout
        assert "GLM_BASE_URL" not in p.read_text()


def test_idempotent_and_untouched_profiles():
    with tempfile.TemporaryDirectory() as td:
        pinned = Path(td, "profiles/worker-pinned/.env")
        pinned.parent.mkdir(parents=True)
        pinned.write_text("GLM_API_KEY=k\nGLM_BASE_URL=http://127.0.0.1:9099\n")
        nokey = Path(td, "profiles/worker-none/.env")
        nokey.parent.mkdir(parents=True)
        nokey.write_text("OTHER=1\n")
        r = _run(td, "--force")
        assert "ok: all worker profiles already pinned" in r.stdout
        assert pinned.read_text().count("GLM_BASE_URL") == 1
        assert "GLM_BASE_URL" not in nokey.read_text()


def test_custom_router_url_is_used():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td, "profiles/worker-x/.env")
        p.parent.mkdir(parents=True)
        p.write_text("GLM_API_KEY=k\n")
        env = dict(os.environ, HERMES_HOME=td, FLEET_ROUTER_URL="http://10.0.0.5:9099")
        subprocess.run(["bash", str(SCRIPT), "--force"], capture_output=True, text=True, env=env)
        assert "GLM_BASE_URL=http://10.0.0.5:9099" in p.read_text()
