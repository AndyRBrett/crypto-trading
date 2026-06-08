import time

from bot.config import Config
from bot.coordinate import Coordinator
from bot.engine import Engine
from tests.test_engine import FakeExplainer, FakeStorage


def make_coord():
    cfg = Config()
    cfg.coordinate_enabled = True
    cfg.github_token = "tok"
    cfg.publish_repo = "owner/repo"
    return Coordinator(cfg)


def test_coordinator_disabled_without_token():
    cfg = Config()
    cfg.coordinate_enabled = True
    cfg.publish_repo = "owner/repo"  # but no token
    assert Coordinator(cfg).enabled is False


def test_remote_db_path_per_account_and_default():
    c = make_coord()
    # Named accounts get their own remote DB; the synthesized "default" keeps
    # using the legacy state_db_path so existing cloud history isn't orphaned.
    assert c._remote_db_path("trend") == "trading.trend.db"
    assert c._remote_db_path("default") == c.config.state_db_path


def test_pull_push_db_for_target_account_paths(monkeypatch, tmp_path):
    c = make_coord()
    seen = {"get": [], "put": []}
    monkeypatch.setattr(c, "_get_file", lambda path: (seen["get"].append(path) or (b"data", "sha1")))
    monkeypatch.setattr(c, "_put_file", lambda path, data, msg: seen["put"].append(path) or True)

    local = tmp_path / "trading.meanrev.db"
    assert c.pull_db_for("meanrev", str(local)) is True
    assert local.read_bytes() == b"data"
    assert seen["get"] == ["trading.meanrev.db"]

    assert c.push_db_for("meanrev", str(local)) is True
    assert seen["put"] == ["trading.meanrev.db"]


def test_laptop_active_fresh_local_lease(monkeypatch):
    c = make_coord()
    monkeypatch.setattr(c, "read_lease", lambda: {"driver": "local", "heartbeat": time.time()})
    assert c.laptop_active() is True


def test_laptop_active_false_when_stale(monkeypatch):
    c = make_coord()
    monkeypatch.setattr(c, "read_lease", lambda: {"driver": "local", "heartbeat": time.time() - 99999})
    assert c.laptop_active() is False


def test_laptop_active_false_for_cloud_lease(monkeypatch):
    c = make_coord()
    monkeypatch.setattr(c, "read_lease", lambda: {"driver": "cloud", "heartbeat": time.time()})
    assert c.laptop_active() is False


def test_laptop_active_false_without_lease(monkeypatch):
    c = make_coord()
    monkeypatch.setattr(c, "read_lease", lambda: None)
    assert c.laptop_active() is False


class FakeCoord:
    def __init__(self, active):
        self.enabled = True
        self._active = active
        self.claimed = False
        self.pushed = False

    def laptop_active(self):
        return self._active

    def claim_lease(self):
        self.claimed = True
        return True

    def pull_db(self, path):
        return False

    def push_db(self, path):
        self.pushed = True
        return True


def _engine(coord, **cfg_overrides):
    cfg = Config()
    for k, v in cfg_overrides.items():
        setattr(cfg, k, v)
    return Engine(
        cfg,
        market_data=object(),  # must not be touched if standing down
        storage=FakeStorage(),
        explainer=FakeExplainer(),
        coordinator=coord,
    )


def test_cloud_stands_down_when_laptop_active():
    coord = FakeCoord(active=True)
    eng = _engine(coord, driver_role="cloud")
    assert eng.tick() == []
    assert coord.claimed is False  # stood down before claiming the lease


def test_cloud_runs_and_claims_when_laptop_idle():
    coord = FakeCoord(active=False)
    # No products -> tick does nothing but should still claim the lease.
    eng = _engine(coord, driver_role="cloud", products=[])
    assert eng.tick() == []
    assert coord.claimed is True
