"""Publish the dashboard state to a GitHub Pages branch via the GitHub API.

This is what makes the dashboard viewable on your phone without running a
server: the static dashboard lives on the ``gh-pages`` branch (kept in sync by
a workflow), and the bot PUTs the latest ``state.json`` to that branch on every
tick. GitHub Pages serves it at https://<user>.github.io/<repo>/ .

Optional and off by default. Needs a GitHub token with contents:write on the
repo (set ``GITHUB_TOKEN`` in .env). Every failure path is non-fatal — the bot
keeps trading and writing local state regardless.
"""

from __future__ import annotations

import base64
import logging

import requests

log = logging.getLogger(__name__)

API = "https://api.github.com"


class Publisher:
    def __init__(self, config):
        self.config = config
        self.enabled = bool(
            config.publish_enabled and config.github_token and config.publish_repo
        )
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
        self._sha: str | None = None  # cached SHA of the remote file

    def _url(self) -> str:
        return f"{API}/repos/{self.config.publish_repo}/contents/{self.config.publish_path}"

    def _get_sha(self) -> str | None:
        try:
            r = self._session.get(
                self._url(), params={"ref": self.config.publish_branch}, timeout=15
            )
            if r.status_code == 200:
                return r.json().get("sha")
        except Exception as exc:  # pragma: no cover - network guard
            log.warning("publish: could not fetch existing SHA: %s", exc)
        return None

    def _put(self, payload: dict) -> requests.Response:
        return self._session.put(self._url(), json=payload, timeout=20)

    def publish(self, state_path: str) -> bool:
        """Upload ``state_path`` to the configured branch. Returns success."""
        if not self.enabled:
            return False
        try:
            with open(state_path, "rb") as f:
                content = f.read()
        except OSError as exc:
            log.warning("publish: cannot read %s: %s", state_path, exc)
            return False

        sha = self._sha or self._get_sha()
        payload = {
            "message": "Update dashboard state",
            "content": base64.b64encode(content).decode("ascii"),
            "branch": self.config.publish_branch,
        }
        if sha:
            payload["sha"] = sha

        try:
            r = self._put(payload)
            # A stale SHA (someone/something else updated the file) -> refetch once.
            if r.status_code in (409, 422) and "sha" in payload:
                self._sha = self._get_sha()
                if self._sha:
                    payload["sha"] = self._sha
                else:
                    payload.pop("sha", None)
                r = self._put(payload)

            if r.status_code in (200, 201):
                self._sha = r.json().get("content", {}).get("sha")
                log.info("Published dashboard state to %s", self.config.publish_branch)
                return True
            log.warning("publish failed: HTTP %s %s", r.status_code, r.text[:200])
        except Exception as exc:  # pragma: no cover - network guard
            log.warning("publish error: %s", exc)
        return False
