"""Generate a matching VAPID keypair for Web Push.

The private and public halves MUST come from the same keypair: a browser
subscription is permanently bound to the public key it subscribed with, and the
push service rejects any message signed by a different key
(``VapidPkHashMismatch``). Generating them separately — or rotating one without
the other — silently breaks every existing subscription.

    python -m scripts.vapid_keygen

Put the private key in the VAPID_PRIVATE_KEY secret (or .env). The bot derives
the public half from it automatically and publishes it in state.json, so the
dashboard always subscribes with the matching key — you do not need to paste the
public key anywhere.

After rotating the key, every existing subscription is dead: re-subscribe from
the dashboard and replace the PUSH_SUBSCRIPTION secret with the new JSON.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ec

from bot.notifier import _b64e, derive_public_key

priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
private_b64 = _b64e(priv.private_numbers().private_value.to_bytes(32, "big"))

print("VAPID keypair generated.\n")
print("VAPID_PRIVATE_KEY (secret — never commit this):")
print(f"  {private_b64}\n")
print("Matching public key (derived automatically by the bot, shown for reference):")
print(f"  {derive_public_key(private_b64)}\n")
print("Next steps:")
print("  1. Set VAPID_PRIVATE_KEY to the value above (repo secret, or .env locally).")
print("  2. Re-subscribe from the dashboard bell — any older subscription is now dead.")
print("  3. Replace the PUSH_SUBSCRIPTION secret with the new JSON.")
