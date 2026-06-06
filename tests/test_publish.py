import base64
import json
import tempfile
from pathlib import Path

from bot.config import Config
from bot.publish import Publisher


def _state_file(tmp):
    p = Path(tmp) / "state.json"
    p.write_text(json.dumps({"equity": 123.45}))
    return str(p)


def test_disabled_without_token():
    cfg = Config()
    cfg.publish_enabled = True
    cfg.publish_repo = "owner/repo"
    # no github_token -> stays disabled
    pub = Publisher(cfg)
    assert pub.enabled is False
    with tempfile.TemporaryDirectory() as tmp:
        assert pub.publish(_state_file(tmp)) is False


def test_disabled_when_flag_off():
    cfg = Config()
    cfg.github_token = "tok"
    cfg.publish_repo = "owner/repo"
    cfg.publish_enabled = False
    assert Publisher(cfg).enabled is False


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


def test_publish_puts_base64_content(monkeypatch):
    cfg = Config()
    cfg.publish_enabled = True
    cfg.github_token = "tok"
    cfg.publish_repo = "owner/repo"
    pub = Publisher(cfg)
    assert pub.enabled is True

    calls = {}

    def fake_get_sha():
        return None  # file doesn't exist yet -> create

    def fake_put(payload):
        calls["payload"] = payload
        return FakeResp(201, {"content": {"sha": "newsha"}})

    monkeypatch.setattr(pub, "_get_sha", fake_get_sha)
    monkeypatch.setattr(pub, "_put", fake_put)

    with tempfile.TemporaryDirectory() as tmp:
        ok = pub.publish(_state_file(tmp))

    assert ok is True
    payload = calls["payload"]
    assert payload["branch"] == "gh-pages"
    decoded = json.loads(base64.b64decode(payload["content"]))
    assert decoded["equity"] == 123.45
    assert pub._sha == "newsha"  # cached for next call


def test_publish_retries_on_stale_sha(monkeypatch):
    cfg = Config()
    cfg.publish_enabled = True
    cfg.github_token = "tok"
    cfg.publish_repo = "owner/repo"
    pub = Publisher(cfg)
    pub._sha = "stale"

    seq = [FakeResp(409, {"message": "conflict"}), FakeResp(200, {"content": {"sha": "fresh"}})]
    monkeypatch.setattr(pub, "_get_sha", lambda: "refetched")
    monkeypatch.setattr(pub, "_put", lambda payload: seq.pop(0))

    with tempfile.TemporaryDirectory() as tmp:
        ok = pub.publish(_state_file(tmp))

    assert ok is True
    assert pub._sha == "fresh"
