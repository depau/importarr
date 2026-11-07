"""Apprise notifications (no-op logging when APPRISE_URL is unset)."""
from __future__ import annotations

import asyncio
import logging

import apprise

log = logging.getLogger("importarr.notify")


class Notifier:
    def __init__(self, apprise_url: str | None):
        self._apprise = apprise.Apprise() if apprise_url else None
        if self._apprise:
            ok = self._apprise.add(apprise_url)
            if not ok:
                log.warning("failed to configure apprise URL, notifications disabled")
                self._apprise = None

    def send(self, title: str, body: str) -> None:
        if not self._apprise:
            log.info("notify: %s — %s", title, body.replace("\n", " | "))
            return
        try:
            self._apprise.notify(title=title, body=body, body_format="text")
        except Exception:
            log.exception("notification failed: %s", title)

    async def notify(self, title: str, body: str) -> None:
        await asyncio.to_thread(self.send, title, body)
