import builtins
import gettext
import locale
import os
import sys

try:
    import polib
except Exception:
    polib = None

AUTO_LANGUAGE_CODE = "auto"
FALLBACK_LANGUAGE_CODE = "en"
SUPPORTED_LANGUAGE_CODES = ("fr", "en")
LANGUAGE_NAME_MSGIDS = {
    "fr": "French",
    "en": "English",
}

CURRENT_LANGUAGE_CODE = FALLBACK_LANGUAGE_CODE
CURRENT_LANGUAGE_SOURCE = "source"


def install_language(preferred_lang=AUTO_LANGUAGE_CODE, prefer_po=True):
    global CURRENT_LANGUAGE_CODE
    global CURRENT_LANGUAGE_SOURCE

    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    locales_dir = os.path.join(base_path, 'locales')
    lang_code = resolve_language(preferred_lang)

    if lang_code == FALLBACK_LANGUAGE_CODE:
        _install_identity_translation()
        CURRENT_LANGUAGE_CODE = FALLBACK_LANGUAGE_CODE
        CURRENT_LANGUAGE_SOURCE = 'source'
        return FALLBACK_LANGUAGE_CODE, 'source'

    # In development, load .po directly to avoid manual compile steps.
    if prefer_po and not getattr(sys, 'frozen', False):
        if _install_from_po(locales_dir, lang_code):
            CURRENT_LANGUAGE_CODE = lang_code
            CURRENT_LANGUAGE_SOURCE = 'po'
            return lang_code, 'po'

    try:
        lang = gettext.translation('base', localedir=locales_dir, languages=[lang_code])
        lang.install()
        CURRENT_LANGUAGE_CODE = lang_code
        CURRENT_LANGUAGE_SOURCE = 'mo'
        return lang_code, 'mo'
    except Exception:
        _install_identity_translation()
        CURRENT_LANGUAGE_CODE = FALLBACK_LANGUAGE_CODE
        CURRENT_LANGUAGE_SOURCE = 'source'
        return FALLBACK_LANGUAGE_CODE, 'source'


def get_current_language_code():
    return CURRENT_LANGUAGE_CODE


def get_current_language_source():
    return CURRENT_LANGUAGE_SOURCE


def get_supported_language_codes():
    return SUPPORTED_LANGUAGE_CODES


def get_language_name_msgid(language_code):
    return LANGUAGE_NAME_MSGIDS.get(str(language_code or "").strip().lower())


def get_language_display_name(language_code):
    msgid = get_language_name_msgid(language_code)
    if msgid:
        return builtins.__dict__.get("_", lambda s: s)(msgid)
    return str(language_code or "").strip().lower() or FALLBACK_LANGUAGE_CODE


def normalize_ui_language(preferred_lang):
    normalized = str(preferred_lang or AUTO_LANGUAGE_CODE).strip().lower()
    if normalized == AUTO_LANGUAGE_CODE:
        return AUTO_LANGUAGE_CODE
    if normalized in SUPPORTED_LANGUAGE_CODES:
        return normalized
    return AUTO_LANGUAGE_CODE


def get_system_language_code():
    try:
        sys_lang = locale.getlocale()[0] or locale.getdefaultlocale()[0]
    except Exception:
        sys_lang = None

    if sys_lang and sys_lang.lower().startswith('fr'):
        return 'fr'
    return FALLBACK_LANGUAGE_CODE


def resolve_language(preferred_lang):
    normalized = normalize_ui_language(preferred_lang)
    if normalized == AUTO_LANGUAGE_CODE:
        return get_system_language_code()
    return normalized


def _install_identity_translation():
    builtins.__dict__['_'] = lambda s: s


def _install_from_po(locales_dir, lang_code):
    if polib is None:
        return False

    po_path = os.path.join(locales_dir, lang_code, 'LC_MESSAGES', 'base.po')
    if not os.path.exists(po_path):
        return False

    try:
        po = polib.pofile(po_path)
        mapping = {}
        for entry in po:
            if entry.obsolete:
                continue
            if entry.msgid_plural:
                mapping[entry.msgid] = entry.msgstr_plural.get('0', entry.msgid)
            elif entry.msgstr:
                mapping[entry.msgid] = entry.msgstr

        builtins.__dict__['_'] = lambda s: mapping.get(s, s)
        return True
    except Exception:
        return False
