"""Send a test push notification to verify VAPID_PRIVATE_KEY + PUSH_SUBSCRIPTION are wired up.

Exits non-zero when the push service rejects the message. The earlier version
printed "Done — check your phone" and exited 0 no matter what the push service
said, so a rejected message looked exactly like a delivered one — the run that
hid a six-week VapidPkHashMismatch outage was green.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.notifier import Notifier, derive_public_key

# Without this the notifier's log records go through logging's "last resort"
# handler, which writes to stderr unbuffered while print() buffers — so the
# failure appeared ABOVE the "Sending…" line and read like unrelated noise.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)

sub = os.environ.get("PUSH_SUBSCRIPTION", "")
key = os.environ.get("VAPID_PRIVATE_KEY", "")

if not sub:
    print("ERROR: PUSH_SUBSCRIPTION secret is not set or empty.")
    sys.exit(1)
if not key:
    print("ERROR: VAPID_PRIVATE_KEY secret is not set or empty.")
    sys.exit(1)

public = derive_public_key(key)
if not public:
    print("ERROR: VAPID_PRIVATE_KEY is not a valid base64url P-256 private scalar.")
    sys.exit(1)

notifier = Notifier(sub, key)
if not notifier.enabled:
    print("ERROR: Notifier failed to initialise — check that PUSH_SUBSCRIPTION is valid JSON.")
    sys.exit(1)

print(f"Signing with VAPID public key: {public}")
print("Sending test notification…")
ok = notifier.send(
    title="CryptoBot ✅ notifications working",
    message="Test successful. You'll be notified on every close, and weekly even when idle.",
    priority="high",
)

if not ok:
    print("\nFAILED — the push service did not accept the message.")
    print(notifier.last_error)
    sys.exit(1)

print("Delivered — the push service accepted the message. Check your phone.")
