"""Driver coordination + shared portfolio across the laptop and the cloud.

The problem: GitHub's scheduled runs are infrequent, but a laptop running the
loop can tick every 15 min. We want the laptop to take priority when it's on,
the cloud to cover when it's off, and a *single* continuous paper portfolio
across both.

How it works (all via the GitHub API, on the shared ``state_branch``):
  * ``trading.db`` is the shared portfolio. Both drivers pull it at startup and
    push it after each tick, so P&L is continuous regardless of who's driving.
  * ``driver.json`` is a lease: ``{driver, host, heartbeat}``. The laptop
    refreshes it every tick; the cloud stands down while a *local* lease is
    fresh (within ``lease_ttl_seconds``) and resumes once it goes stale.

Everything here is best-effort and non-fatal: on any failure the bot keeps
trading on its local database.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"


class Coordinator:
    def __init__(self, config):
        self.config = config
        self.enabled = bool(
            config.coordinate_enabled and config.github_token and config.publish_repo
        )
        self.role = config.driver_role  # "local" or "cloud"
        self.ttl = config.lease_ttl_seconds
        self.branch = config.state_branch
        self._session = requests.Session()
        if config.github_token:
            self._session.headers.update(
                {
                    "Authorization": f"Bearer {config.github_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "crypto-paper-bot/0.1",
                }
            )
        self._sha: dict[str, str | None] = {}

    # -- low-level file ops on the state branch ----------------------------

    def _url(self, path: str) -> str:
        return f"{API}/repos/{self.config.publish_repo}/contents/{path}"

    def _get_file(self, path: str):
        """Return (bytes | None, sha | None) for a file on the state branch."""
        try:
            r = self._session.get(self._url(path), params={"ref": self.branch}, timeout=20)
            if r.status_code == 404:
                return None, None
            if r.status_code == 200:
                body = r.json()
                sha = body.get("sha")
                content = body.get("content")
                if content:
                    return base64.b64decode(content), sha
                dl = body.get("download_url")
                if dl:  # large files aren't inlined; fetch the raw URL
                    raw = self._session.get(dl, timeout=30)
                    if raw.ok:
                        return raw.content, sha
                return None, sha
            log.warning("coordinate: GET %s -> HTTP %s", path, r.status_code)
        except Exception as exc:  # pragma: no cover - network guard
            log.warning("coordinate: GET %s failed: %s", path, exc)
        return None, None

    def _put_file(self, path: str, data: bytes, message: str) -> bool:
        payload = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        sha = self._sha.get(path)
        if sha:
            payload["sha"] = sha
        try:
            r = self._session.put(self._url(path), json=payload, timeout=25)
            if r.status_code in (409, 422):  # stale/missing sha -> refetch once
                _, fresh = self._get_file(path)
                if fresh:
                    payload["sha"] = fresh
                else:
                    payload.pop("sha", None)
                r = self._session.put(self._url(path), json=payload, timeout=25)
            if r.status_code in (200, 201):
                self._sha[path] = r.json().get("content", {}).get("sha")
                return True
            log.warning("coordinate: PUT %s -> HTTP %s %s", path, r.status_code, r.text[:160])
        except Exception as exc:  # pragma: no cover - network guard
            log.warning("coordinate: PUT %s failed: %s", path, exc)
        return False

    # -- portfolio (shared trading.db) -------------------------------------

    def pull_db(self, local_path: str) -> bool:
        """Replace the local DB with the shared one. Call before opening sqlite."""
        if not self.enabled:
            return False
        data, sha = self._get_file(self.config.state_db_path)
        self._sha[self.config.state_db_path] = sha
        if data is None:
            log.info("coordinate: no shared portfolio yet; starting fresh.")
            return False
        try:
            with open(local_path, "wb") as f:
                f.write(data)
        except OSError as exc:
            log.warning("coordinate: could not write %s: %s", local_path, exc)
            return False
        log.info("coordinate: pulled shared portfolio from %s.", self.branch)
        return True

    def push_db(self, local_path: str) -> bool:
        if not self.enabled:
            return False
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except OSError as exc:
            log.warning("coordinate: could not read %s: %s", local_path, exc)
            return False
        return self._put_file(self.config.state_db_path, data, "Update portfolio state [skip ci]")

    # -- per-account portfolios (multi-account) ----------------------------

    def _remote_db_path(self, account_name: str) -> str:
        """Remote path on the state branch for an account's DB.

        The synthesized single "default" account keeps using the legacy
        ``state_db_path`` ("trading.db") so existing cloud history isn't orphaned.
        """
        if account_name == "default":
            return self.config.state_db_path
        return f"trading.{account_name}.db"

    def pull_db_for(self, account_name: str, local_path: str) -> bool:
        """Pull one account's shared DB. Call before opening its sqlite file."""
        if not self.enabled:
            return False
        remote = self._remote_db_path(account_name)
        data, sha = self._get_file(remote)
        self._sha[remote] = sha
        if data is None:
            log.info("coordinate: no shared portfolio yet for %s.", account_name)
            return False
        try:
            with open(local_path, "wb") as f:
                f.write(data)
        except OSError as exc:
            log.warning("coordinate: could not write %s: %s", local_path, exc)
            return False
        log.info("coordinate: pulled shared portfolio for %s.", account_name)
        return True

    def push_db_for(self, account_name: str, local_path: str) -> bool:
        if not self.enabled:
            return False
        remote = self._remote_db_path(account_name)
        try:
            with open(local_path, "rb") as f:
                data = f.read()
        except OSError as exc:
            log.warning("coordinate: could not read %s: %s", local_path, exc)
            return False
        return self._put_file(remote, data, f"Update {account_name} portfolio [skip ci]")

    # -- lease (driver.json) ----------------------------------------------

    def read_lease(self) -> dict | None:
        data, sha = self._get_file(self.config.lease_path)
        self._sha[self.config.lease_path] = sha
        if not data:
            return None
        try:
            return json.loads(data)
        except ValueError:
            return None

    def laptop_active(self) -> bool:
        """True if a *local* driver lease is still fresh (used by the cloud)."""
        lease = self.read_lease()
        if not lease:
            return False
        if lease.get("driver") != "local":
            return False
        age = time.time() - float(lease.get("heartbeat", 0))
        return 0 <= age < self.ttl

    def claim_lease(self) -> bool:
        """Write/refresh the lease for this driver (heartbeat = now)."""
        body = json.dumps(
            {"driver": self.role, "host": socket.gethostname(), "heartbeat": time.time()},
            indent=2,
        ).encode("utf-8")
        return self._put_file(self.config.lease_path, body, "Update driver lease [skip ci]")
