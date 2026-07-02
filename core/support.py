"""Support report client for Accessible Media Editor.

POSTs a user-written support report to the shared app-backend
(``/api/feedback/report``), authenticated with the per-app Bearer secret. The
app id is ``ame`` and the bearer is loaded from the gitignored ``core/_secrets``
module — when it is absent/empty the feature stays dormant (sends will fail with
a clear message). Same wire contract as the AMC/DownAccess clients.
"""

import io
import json
import platform
import re
import sys
import uuid
from urllib import error, request

from core.app_info import (
    APP_EXE_NAME,
    APP_NAME,
    APP_VERSION,
)
from core.i18n import get_current_language_code

# Bearer secret — loaded from the gitignored core/_secrets.py (copy from
# core/_secrets.example.py). Empty when the backend isn't wired yet: the client
# stays dormant and send_support_report reports a clear server error.
try:
    from core._secrets import SUPPORT_BEARER as _BEARER
except ImportError:
    _BEARER = ""


def N_(s):
    """Marker for gettext extraction. Returns the string unchanged."""
    return s


# Report backend — generic multi-app endpoint on the shared app-backend (Go).
FEEDBACK_REPORT_URL = "https://mathieumartin.ovh/api/feedback/report"
_APP_ID = "ame"
_MAX_SUMMARY = 100_000

SUPPORT_HTTP_TIMEOUT_SECONDS = 15
SUPPORT_ISSUE_TYPE_ITEMS = (
    ("cut_export_problem", N_("Cut / export problem")),
    ("playback_problem", N_("Playback problem")),
    ("application_crash", N_("Application crash")),
    ("update_problem", N_("Update problem")),
    ("accessibility_issue", N_("Accessibility issue")),
    ("installation_problem", N_("Installation problem")),
    ("feature_request", N_("Feature request")),
    ("other", N_("Other")),
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SupportSendError(Exception):
    def __init__(self, error_code, message):
        super().__init__(message)
        self.error_code = error_code
        self.message = message


def get_support_issue_type_items():
    return tuple(SUPPORT_ISSUE_TYPE_ITEMS)


def get_support_issue_type_codes():
    return tuple(code for code, _ in SUPPORT_ISSUE_TYPE_ITEMS)


def build_support_issue_label(issue_type):
    labels = {code: _(msgid) for code, msgid in SUPPORT_ISSUE_TYPE_ITEMS}
    return labels.get(issue_type, _("Other"))


def collect_support_context(window):
    """Read a diagnostic snapshot from the editor frame (defensive: every field
    has a fallback, so a partially-built or unusual frame never breaks a report)."""
    settings = getattr(window, "_settings", {}) or {}
    meta = getattr(window, "meta", None)

    loaded_file = ""
    media_duration_s = 0.0
    has_video = False
    if meta is not None:
        loaded_file = str(getattr(meta, "full_path", "") or "")
        loaded_file = loaded_file.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        try:
            media_duration_s = float(getattr(meta, "duration", 0) or 0)
        except (TypeError, ValueError):
            media_duration_s = 0.0
        has_video = bool(getattr(meta, "has_video", False))

    plan = getattr(window, "plan", None)
    segment_count = len(getattr(plan, "segments", []) or []) if plan is not None else 0

    return {
        "app_version": APP_VERSION,
        "execution_mode": "packaged" if getattr(sys, "frozen", False) else "source",
        "operating_system": f"{platform.system()} {platform.release()}".strip(),
        "language": get_current_language_code(),
        "loaded_file": loaded_file,
        "media_duration_s": round(media_duration_s, 3),
        "has_video": has_video,
        "segment_count": segment_count,
        "auto_update_check_enabled": bool(settings.get("check_updates_on_startup", True)),
    }


def validate_support_email(email_address):
    return bool(EMAIL_PATTERN.match(str(email_address or "").strip()))


def validate_support_form(email_address, issue_type, user_message):
    if not validate_support_email(email_address):
        return _("Please enter a valid email address.")
    if issue_type not in get_support_issue_type_codes():
        return _("Please choose an issue type.")
    if not str(user_message or "").strip():
        return _("Please describe your issue before sending the report.")
    return ""


def build_support_subject(issue_type, context=None):
    context = context or {}
    version = str(context.get("app_version") or APP_VERSION)
    issue_label = build_support_issue_label(issue_type)
    return _("{app_name} - {issue} - v{version}").format(
        app_name=APP_NAME,
        issue=issue_label,
        version=version,
    )


def build_support_technical_block(context):
    """Translated key/value block shown in the in-app preview."""
    lines = [
        _("App version: {value}").format(value=context.get("app_version", APP_VERSION)),
        _("Execution mode: {value}").format(
            value=_format_execution_mode(context.get("execution_mode", "source"))
        ),
        _("Operating system: {value}").format(
            value=context.get("operating_system", _("Unknown"))
        ),
        _("Language: {value}").format(value=context.get("language", _("Unknown"))),
        _("Loaded file: {value}").format(value=context.get("loaded_file") or _("None")),
        _("Media duration (s): {value}").format(value=context.get("media_duration_s", 0)),
        _("Has video: {value}").format(value=_format_bool(context.get("has_video", False))),
        _("Segments: {value}").format(value=context.get("segment_count", 0)),
        _("Automatic update checks: {value}").format(
            value=_format_bool(context.get("auto_update_check_enabled", False))
        ),
    ]
    return "\n".join(lines)


def build_support_report(email_address, issue_type, user_message, context):
    message = str(user_message or "").strip() or _("Please describe your issue here.")
    return "\n".join(
        [
            _("Issue type: {value}").format(value=build_support_issue_label(issue_type)),
            _("User email: {value}").format(value=str(email_address or "").strip()),
            "",
            _("Your message:"),
            message,
            "",
            _("Technical information:"),
            build_support_technical_block(context),
        ]
    )


def send_support_report(email_address, issue_type, user_message, context, debug_log="",
                        timeout=SUPPORT_HTTP_TIMEOUT_SECONDS):
    """Send a support report to the generic /api/feedback/report endpoint.

    Multipart `report` (JSON) + optional `log_file`, authenticated with the per-app
    Bearer secret. The technical block is built here as a French key/value section
    (hardcoded — the email goes to the developer, not the user, so it must not
    follow the UI language).
    """
    email = str(email_address or "").strip()
    summary = str(user_message or "").strip()
    context = dict(context or {})
    version = str(context.get("app_version") or APP_VERSION)

    report = {
        "app": _APP_ID,
        "email": email[:200],
        "summary": summary[:_MAX_SUMMARY],
        "subject_hint": f"{_fr_issue_label(issue_type)} — v{version}",
        "sections": {
            "Informations techniques": _build_support_fr_section(issue_type, context),
        },
    }

    fields = {"report": json.dumps(report, ensure_ascii=False)}
    files = {}
    debug_log = str(debug_log or "")
    if debug_log:
        files["log_file"] = ("debug.log", debug_log)
    data, content_type = _make_multipart(fields, files)

    api_request = request.Request(
        FEEDBACK_REPORT_URL,
        data=data,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": content_type,
            "Authorization": f"Bearer {_BEARER}",
            "User-Agent": f"{APP_EXE_NAME}/{APP_VERSION}",
        },
    )

    try:
        with request.urlopen(api_request, timeout=timeout) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise _build_support_send_error(exc) from exc
    except error.URLError as exc:
        raise SupportSendError(
            "server_error",
            _("Unable to contact the support server right now."),
        ) from exc
    except TimeoutError as exc:
        raise SupportSendError(
            "server_error",
            _("The support request timed out."),
        ) from exc
    except json.JSONDecodeError as exc:
        raise SupportSendError(
            "server_error",
            _("The support server returned an invalid response."),
        ) from exc

    if not isinstance(response_payload, dict):
        raise SupportSendError(
            "server_error",
            _("The support server returned an invalid response."),
        )

    if response_payload.get("ok"):
        return response_payload

    raise SupportSendError(
        str(response_payload.get("error_code") or "server_error"),
        _map_support_error_message(
            str(response_payload.get("error_code") or "server_error"),
            str(response_payload.get("message") or ""),
        ),
    )


def _build_support_send_error(exc):
    message = _("Unable to send the support report right now.")
    error_code = "server_error"

    try:
        response_payload = json.loads(exc.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        response_payload = {}

    if isinstance(response_payload, dict):
        error_code = str(response_payload.get("error_code") or error_code)
        message = _map_support_error_message(
            error_code,
            str(response_payload.get("message") or ""),
        )
    elif getattr(exc, "code", 0) == 429:
        error_code = "rate_limited"
        message = _map_support_error_message(error_code, "")

    return SupportSendError(error_code, message)


def _map_support_error_message(error_code, fallback_message):
    mapping = {
        "validation_error": _("Please review the support form fields and try again."),
        "invalid_json": _("Please review the support form fields and try again."),
        "unauthorized": _("Unable to send the support report right now."),
        "rate_limited": _("Too many reports have been sent recently. Please try again later."),
        "server_error": _("Unable to send the support report right now."),
    }
    return mapping.get(error_code) or fallback_message or mapping["server_error"]


def _make_multipart(fields, files):
    """Build a multipart/form-data body, return (body_bytes, content_type)."""
    boundary = uuid.uuid4().hex
    body = io.BytesIO()

    for name, value in fields.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        body.write(f"{value}\r\n".encode())

    for name, (filename, content) in files.items():
        body.write(f"--{boundary}\r\n".encode())
        body.write(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode()
        )
        body.write(content if isinstance(content, bytes) else content.encode("utf-8"))
        body.write(b"\r\n")

    body.write(f"--{boundary}--\r\n".encode())
    return body.getvalue(), f"multipart/form-data; boundary={boundary}"


# --- French technical section (hardcoded) ------------------------------------
# The report email goes to the developer, so its technical block is built in
# French regardless of the UI language.

_FR_ISSUE_LABELS = {
    "cut_export_problem": "Problème de découpe / export",
    "playback_problem": "Problème de lecture",
    "application_crash": "Crash de l'application",
    "update_problem": "Problème de mise à jour",
    "accessibility_issue": "Problème d'accessibilité",
    "installation_problem": "Problème d'installation",
    "feature_request": "Demande de fonctionnalité",
    "other": "Autre",
}


def _fr_issue_label(issue_type):
    return _FR_ISSUE_LABELS.get(issue_type, _FR_ISSUE_LABELS["other"])


def _build_support_fr_section(issue_type, context):
    """Ordered key/value dict of the technical context, in hardcoded French."""
    return {
        "Type de demande": _fr_issue_label(issue_type),
        "Version de l'application": _fr_str(context, "app_version", "inconnue"),
        "Mode d'exécution": _fr_execution_mode(context.get("execution_mode", "source")),
        "Système d'exploitation": _fr_str(context, "operating_system", "inconnu"),
        "Langue": _fr_str(context, "language", "inconnue"),
        "Fichier chargé": _fr_str(context, "loaded_file", "aucun"),
        "Durée du média (s)": _fr_str(context, "media_duration_s", "0"),
        "Contient de la vidéo": _fr_bool(context.get("has_video", False)),
        "Nombre de segments": _fr_str(context, "segment_count", "0"),
        "Vérification auto des mises à jour": _fr_bool(context.get("auto_update_check_enabled", False)),
    }


def _fr_str(context, key, fallback):
    value = context.get(key)
    if value is None or value == "":
        return fallback
    return str(value).strip()


def _fr_execution_mode(value):
    return "packagé" if value == "packaged" else "source"


def _fr_bool(value):
    if isinstance(value, str):
        return "oui" if value.strip().lower() in ("1", "true", "on", "yes") else "non"
    return "oui" if bool(value) else "non"


def _format_execution_mode(value):
    return _("Packaged") if value == "packaged" else _("Source")


def _format_bool(value):
    return _("Yes") if bool(value) else _("No")
