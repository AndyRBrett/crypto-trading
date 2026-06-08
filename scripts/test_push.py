"""Send a test push notification to verify VAPID_PRIVATE_KEY + PUSH_SUBSCRIPTION are wired up."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.notifier import Notifier

sub = os.environ.get("PUSH_SUBSCRIPTION", "")
key = os.environ.get("VAPID_PRIVATE_KEY", "")

if not sub:
    print("ERROR: PUSH_SUBSCRIPTION secret is not set or empty.")
    sys.exit(1)
if not key:
    print("ERROR: VAPID_PRIVATE_KEY secret is not set or empty.")
    sys.exit(1)

notifier = Notifier(sub, key)
if not notifier.enabled:
    print("ERROR: Notifier failed to initialise — check that PUSH_SUBSCRIPTION is valid JSON.")
    sys.exit(1)

print("Sending test notification…")
notifier.send(
    title="CryptoBot ✅ notifications working",
    message="Test successful. You'll be notified on profits and new portfolio highs.",
    priority="high",
)
print("Done — check your phone.")
