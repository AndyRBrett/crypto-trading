"""Tests for Web Push delivery reporting and VAPID key derivation.

The failure these cover cost six weeks of notifications: the push service was
rejecting every message with ``VapidPkHashMismatch`` while ``send()`` swallowed
the error and the test script exited 0, so a dead channel looked healthy.
"""

import base64

import pytest
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from bot.notifier import Notifier, _b64e, derive_public_key

SUB = (
    '{"endpoint": "https://push.example.com/abc", '
    '"keys": {"p256dh": "%s", "auth": "%s"}}'
)


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    private_b64 = _b64e(priv.private_numbers().private_value.to_bytes(32, "big"))
    public_b64 = _b64e(
        priv.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    return private_b64, public_b64


def _subscription():
    """A subscription JSON with a valid P-256 receiver key, so encryption works."""
    ua = ec.generate_private_key(ec.SECP256R1(), default_backend())
    p256dh = _b64e(
        ua.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    auth = _b64e(b"0123456789abcdef")
    return SUB % (p256dh, auth)


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _patch_post(monkeypatch, response):
    """Intercept the requests.post the notifier imports lazily."""
    import requests

    sent = {}

    def fake_post(endpoint, data=None, headers=None, timeout=None):
        sent["endpoint"] = endpoint
        sent["headers"] = headers
        return response

    monkeypatch.setattr(requests, "post", fake_post)
    return sent


# --- VAPID key derivation -------------------------------------------------

def test_derive_public_key_matches_the_private_half():
    private_b64, public_b64 = _keypair()
    assert derive_public_key(private_b64) == public_b64


def test_derive_public_key_tolerates_missing_base64_padding():
    private_b64, public_b64 = _keypair()
    assert derive_public_key(private_b64.rstrip("=")) == public_b64


def test_derive_public_key_returns_empty_for_junk():
    assert derive_public_key("") == ""
    assert derive_public_key("not-a-key!!") == ""


def test_notifier_exposes_its_public_key():
    private_b64, public_b64 = _keypair()
    assert Notifier(_subscription(), private_b64).public_key == public_b64


# --- delivery reporting ---------------------------------------------------

def test_send_returns_true_when_accepted(monkeypatch):
    private_b64, _ = _keypair()
    n = Notifier(_subscription(), private_b64)
    _patch_post(monkeypatch, FakeResponse(201))
    assert n.send("t", "m") is True
    assert n.last_error == ""


def test_send_reports_vapid_mismatch_with_a_fix(monkeypatch):
    private_b64, _ = _keypair()
    n = Notifier(_subscription(), private_b64)
    _patch_post(monkeypatch, FakeResponse(400, '{"reason":"VapidPkHashMismatch"}'))

    assert n.send("t", "m") is False
    # The operator needs to know the subscription is stale, not just "HTTP 400".
    assert "400" in n.last_error
    assert "re-subscribe" in n.last_error.lower()


@pytest.mark.parametrize("code", [404, 410])
def test_send_reports_expired_subscription(monkeypatch, code):
    private_b64, _ = _keypair()
    n = Notifier(_subscription(), private_b64)
    _patch_post(monkeypatch, FakeResponse(code, "gone"))

    assert n.send("t", "m") is False
    assert "expired" in n.last_error.lower()


def test_send_never_raises_when_the_push_service_is_unreachable(monkeypatch):
    import requests

    private_b64, _ = _keypair()
    n = Notifier(_subscription(), private_b64)

    def boom(*a, **k):
        raise requests.ConnectionError("network down")

    monkeypatch.setattr(requests, "post", boom)
    # A broken notifier must never stop the bot trading...
    assert n.send("t", "m") is False
    # ...but the failure still has to be recoverable by the caller.
    assert "network down" in n.last_error


def test_disabled_notifier_reports_failure_not_success():
    # No subscription -> nothing was delivered, so send() must not claim it was.
    assert Notifier("", "").send("t", "m") is False


def test_clearing_error_after_recovery(monkeypatch):
    private_b64, _ = _keypair()
    n = Notifier(_subscription(), private_b64)
    _patch_post(monkeypatch, FakeResponse(400, '{"reason":"VapidPkHashMismatch"}'))
    n.send("t", "m")
    assert n.last_error

    _patch_post(monkeypatch, FakeResponse(201))
    assert n.send("t", "m") is True
    assert n.last_error == ""
