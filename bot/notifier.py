"""Web Push notifications (RFC 8030/8291/8292) using only `cryptography` + `requests`.

When VAPID_PRIVATE_KEY and PUSH_SUBSCRIPTION are set in the environment the bot
sends encrypted push messages directly to the browser's push service (APNs on
iOS Safari, FCM on Android Chrome).  If either value is absent every method is
a silent no-op so the rest of the bot never needs to guard against a missing
notifier.

Setup is handled from the dashboard: the user clicks "Enable Notifications",
copies the subscription JSON that appears, and adds it as the PUSH_SUBSCRIPTION
GitHub Actions secret.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _b64d(s: str) -> bytes:
    """URL-safe base64 decode, tolerant of missing padding."""
    s = s.rstrip("=")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _hkdf_extract(salt: bytes, ikm: bytes) -> bytes:
    from cryptography.hazmat.primitives import hmac as _hmac, hashes
    from cryptography.hazmat.backends import default_backend
    h = _hmac.HMAC(salt, hashes.SHA256(), backend=default_backend())
    h.update(ikm)
    return h.finalize()


def _hkdf_expand(prk: bytes, info: bytes, length: int) -> bytes:
    from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    return HKDFExpand(
        algorithm=hashes.SHA256(), length=length, info=info, backend=default_backend()
    ).derive(prk)


# ---------------------------------------------------------------------------
# RFC 8291 — message encryption for Web Push
# ---------------------------------------------------------------------------

def _encrypt_push(plaintext: bytes, p256dh: bytes, auth: bytes) -> bytes:
    """Encrypt a push payload per RFC 8291 (aes128gcm content encoding).

    Returns the raw bytes to POST to the push endpoint.
    """
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    backend = default_backend()

    # Ephemeral application-server key pair.
    as_priv = ec.generate_private_key(ec.SECP256R1(), backend)
    as_pub = as_priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    # ECDH with the subscriber's public key.
    ua_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    ecdh_secret = as_priv.exchange(ec.ECDH(), ua_pub)

    # Two-step HKDF key derivation (RFC 8291 §3.3).
    key_info = b"WebPush: info\x00" + p256dh + as_pub
    prk_key = _hkdf_extract(auth, ecdh_secret)
    ikm = _hkdf_expand(prk_key, key_info, 32)

    salt = os.urandom(16)
    prk = _hkdf_extract(salt, ikm)

    cek = _hkdf_expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf_expand(prk, b"Content-Encoding: nonce\x00", 12)

    # Single record: plaintext + record-delimiter 0x02.
    record = plaintext + b"\x02"
    ct = AESGCM(cek).encrypt(nonce, record, None)

    # RFC 8188 content coding header: salt || rs || keyid_len || keyid.
    header = salt + struct.pack(">I", 4096) + struct.pack(">B", len(as_pub)) + as_pub
    return header + ct


# ---------------------------------------------------------------------------
# RFC 8292 — VAPID (Voluntary Application Server Identification)
# ---------------------------------------------------------------------------

def _vapid_headers(raw_key_b64url: str, audience: str, contact: str) -> dict[str, str]:
    """Return Authorization and Crypto-Key headers for a VAPID-signed request."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization, hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    backend = default_backend()
    d = int.from_bytes(_b64d(raw_key_b64url), "big")
    priv = ec.derive_private_key(d, ec.SECP256R1(), backend)
    pub = priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )

    claims = {
        "sub": contact,
        "aud": audience,
        "exp": int(time.time()) + 43_200,  # 12 hours
    }
    hdr_b64 = _b64e(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    pay_b64 = _b64e(json.dumps(claims).encode())
    signing_input = f"{hdr_b64}.{pay_b64}".encode()

    der_sig = priv.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    jwt = f"{hdr_b64}.{pay_b64}.{_b64e(raw_sig)}"

    return {
        "Authorization": f"vapid t={jwt},k={_b64e(pub)}",
    }


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

class Notifier:
    """Sends Web Push notifications to a subscribed browser.

    Requires two environment variables:
      VAPID_PRIVATE_KEY   — base64url-encoded raw P-256 private key (32 bytes)
      PUSH_SUBSCRIPTION   — JSON string from navigator.pushManager.subscribe()

    Both values can be set in your local .env or as GitHub Actions secrets.
    If either is absent every method is a silent no-op.
    """

    def __init__(self, push_subscription: str, vapid_private_key: str, claims_email: str = ""):
        self.enabled = bool(push_subscription and vapid_private_key)
        self._subscription: dict | None = None
        self._private_key = vapid_private_key.strip()
        self._contact = claims_email.strip() or "mailto:bot@example.com"

        if push_subscription:
            try:
                self._subscription = json.loads(push_subscription)
            except Exception:
                log.warning("PUSH_SUBSCRIPTION is not valid JSON — notifications disabled")
                self.enabled = False

    def send(
        self,
        title: str,
        message: str,
        tags: str = "",
        priority: str = "default",
    ) -> None:
        """Send an encrypted Web Push message. Swallows all errors."""
        if not self.enabled or self._subscription is None:
            return
        try:
            import requests as _requests

            sub = self._subscription
            endpoint: str = sub["endpoint"]
            p256dh = _b64d(sub["keys"]["p256dh"])
            auth = _b64d(sub["keys"]["auth"])

            payload = json.dumps({"title": title, "body": message}).encode()
            body = _encrypt_push(payload, p256dh, auth)

            parsed = urlparse(endpoint)
            audience = f"{parsed.scheme}://{parsed.netloc}"
            headers = _vapid_headers(self._private_key, audience, self._contact)
            headers.update({
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": "86400",
            })

            resp = _requests.post(endpoint, data=body, headers=headers, timeout=15)
            if resp.status_code in (200, 201, 202):
                log.info("Push notification sent: %s", title)
            else:
                log.warning("Push returned HTTP %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            log.warning("Push notification failed: %s", exc)
