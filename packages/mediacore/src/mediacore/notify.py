"""ntfy client — ports the publish call from arr-quality-check.sh.

Used for both quality-check *findings* and per-action *status* notifications, so the audit log
and the notifications stay in lock-step (the orchestrator calls :meth:`Notifier.send` from the
same place it records an action).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

from mediacore.models import NtfyConfig

log = logging.getLogger(__name__)

# Priority presets (ntfy 1..5).
PRIORITY_STATUS = 3
PRIORITY_FINDING = 4
PRIORITY_FAILURE = 5


def _header_safe(value: str) -> str:
    """HTTP header values must be latin-1 (requests enforces this). Replace anything else
    (em dashes, CJK titles, emoji) so a notification never raises. The body stays full UTF-8."""
    return value.encode("latin-1", "replace").decode("latin-1")


@dataclass
class NtfyMessage:
    title: str
    body: str
    tags: list[str]
    priority: int = PRIORITY_STATUS
    click: str | None = None
    markdown: bool = True
    topic: str | None = None  # override; defaults to the status topic


class Notifier:
    def __init__(self, cfg: NtfyConfig, *, timeout: float = 15.0) -> None:
        self.cfg = cfg
        self.timeout = timeout

    def send(self, msg: NtfyMessage) -> bool:
        """Publish one message. Returns True on success; never raises (logs and returns False).

        Mirrors the shell behavior: if credentials are missing we skip publishing rather than
        fail the caller — notifications are best-effort and must not abort an action.
        """
        topic = msg.topic or self.cfg.status_topic_resolved()
        url = f"{self.cfg.url.rstrip('/')}/{topic}"
        headers = {
            "X-Title": _header_safe(msg.title),
            "X-Tags": _header_safe(",".join(msg.tags)),
            "X-Priority": str(msg.priority),
        }
        if msg.markdown:
            headers["X-Markdown"] = "yes"
        if msg.click:
            headers["X-Click"] = _header_safe(msg.click)

        auth = None
        if self.cfg.user and self.cfg.password:
            auth = (self.cfg.user, self.cfg.password)
        elif self.cfg.user or self.cfg.password:
            log.warning("ntfy user/password only partially set; skipping publish")
            return False

        try:
            r = requests.post(
                url,
                data=msg.body.encode("utf-8"),
                headers=headers,
                auth=auth,
                timeout=self.timeout,
            )
            r.raise_for_status()
            return True
        except Exception as exc:
            log.warning("ntfy publish failed (%s): %s", url, exc)
            return False
