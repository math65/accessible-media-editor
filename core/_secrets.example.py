"""Template for ``core/_secrets.py`` (which is gitignored).

Copy this file to ``core/_secrets.py`` and set ``SUPPORT_BEARER`` to the AME
backend bearer — the same value as ``AME_BEARER_SECRET`` on the app-backend
server (config/apps.json -> "ame"). Without it, support/announce silently
no-op, so the client stays dormant until the backend is wired.

Generate a fresh secret with:

    python -c "import secrets; print(secrets.token_hex(32))"
"""

SUPPORT_BEARER = ""
