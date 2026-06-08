"""Push notifications via ntfy.sh (https://ntfy.sh).

When a topic is configured the notifier POSTs to ntfy.sh (or a self-hosted
server) so you get a phone push without polling the dashboard.  If no topic is
set every method is a silent no-op, so the rest of the bot never needs to
guard against a missing notifier.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str = ""):
        self.topic = topic.strip()
        self.server = server.rstrip("/")
        self.token = token.strip()
        self.enabled = bool(self.topic)

    def send(
        self,
        title: str,
        message: str,
        tags: str = "",
        priority: str = "default",
    ) -> None:
        """POST a notification. Swallows all errors so a delivery failure
        never interrupts a trading tick."""
        if not self.enabled:
            return

        import requests  # lazy — only needed when notifications are on

        url = f"{self.server}/{self.topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": priority,
            "Content-Type": "text/plain",
        }
        if tags:
            headers["Tags"] = tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            resp = requests.post(url, data=message.encode(), headers=headers, timeout=10)
            resp.raise_for_status()
            log.info("Notification sent: %s", title)
        except Exception as exc:
            log.warning("Notification failed: %s", exc)
