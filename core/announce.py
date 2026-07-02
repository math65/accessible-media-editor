"""Startup announcements for Accessible Media Editor.

Queries the shared app-backend (``/api/announce/check``) and, if an active
announcement exists, hands it to the UI for display, then confirms via
``/api/announce/ack``. Silent check: every network error is swallowed (the
feature must never get in the way of startup). Same client as AMC/DownAccess.
"""
import json
import logging
import threading
import urllib.error
import urllib.request

from core import i18n
from core.support import _APP_ID, _BEARER

log = logging.getLogger("ame.announce")

CHECK_URL = "https://mathieumartin.ovh/api/announce/check"
ACK_URL = "https://mathieumartin.ovh/api/announce/ack"
CLICK_URL = "https://mathieumartin.ovh/api/announce/click"


def _post(url, payload, timeout):
    """POST JSON with Bearer auth, return the decoded body."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Authorization", f"Bearer {_BEARER}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def check_announcement(install_id, on_done):
    """Fetch the active announcement for AME in the background.

    on_done(announcement | None) is called from the thread — use wx.CallAfter on
    the UI side. None = no announcement or a (silently swallowed) error.
    """
    def _run():
        try:
            lang = i18n.get_current_language_code()
            payload = {"app": _APP_ID, "install_id": install_id, "lang": lang}
            body = _post(CHECK_URL, payload, timeout=8)
            ann = body.get("announcement")
            on_done(ann if isinstance(ann, dict) else None)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.debug("Announcement check failed: %s", exc)
            on_done(None)
        except Exception as exc:  # noqa: BLE001
            log.debug("Announcement check: unexpected error: %s", exc)
            on_done(None)

    threading.Thread(target=_run, daemon=True).start()


def ack_announcement(install_id, ann_id):
    """Acknowledge that an announcement was shown (fire-and-forget)."""
    def _run():
        try:
            _post(ACK_URL, {"app": _APP_ID, "install_id": install_id, "id": ann_id}, timeout=8)
        except Exception as exc:  # noqa: BLE001
            log.debug("Announcement ack failed: %s", exc)

    threading.Thread(target=_run, daemon=True).start()


def click_announcement(install_id, ann_id):
    """Record a click on the announcement's link button (fire-and-forget)."""
    def _run():
        try:
            _post(CLICK_URL, {"app": _APP_ID, "install_id": install_id, "id": ann_id}, timeout=8)
        except Exception as exc:  # noqa: BLE001
            log.debug("Announcement click failed: %s", exc)

    threading.Thread(target=_run, daemon=True).start()
