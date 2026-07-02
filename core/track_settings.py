import copy

TRACK_TYPE_CONFIG_KEYS = {
    "video": "video_tracks",
    "audio": "audio_tracks",
    "subtitle": "subtitle_tracks",
}
CONFIG_KEY_TO_TRACK_TYPE = {value: key for key, value in TRACK_TYPE_CONFIG_KEYS.items()}

# Dispositions exposées par type de piste. Établi par recherche sur les sources
# primaires (FFmpeg libavformat/avformat.h + Matroska RFC 9559) : seuls les
# drapeaux réellement *signifiants* pour un type donné sont proposés.
#   - vidéo : seul `default` a un sens grand public sur un flux vidéo.
#   - audio : `visual_impaired` = piste d'audiodescription (clé pour le public
#     aveugle). `descriptions` est un drapeau de TEXTE (description textuelle de
#     la vidéo) — il n'a rien à faire sur l'audio (c'était le bug repéré).
#     `hearing_impaired` n'est PAS proposé sur l'audio (mix « dialogue renforcé »
#     trop rare et trompeur) — réservé aux sous-titres (SME/SDH).
#   - sous-titre : `forced` est réservé aux sous-titres (RFC 9559) ;
#     `hearing_impaired` = SME/SDH.
# Rappel UI (cf. TrackPanel) : pour les sous-titres, seuls les drapeaux "base"
# sont affichés ; les "advanced" des sous-titres restent **préservés depuis la
# source mais masqués** (niche : original/comment/dub/captions/descriptions).
# Pour vidéo/audio, base + advanced sont tous affichés.
BASE_DISPOSITIONS_BY_TYPE = {
    "video": ("default",),
    "audio": ("default", "visual_impaired"),
    "subtitle": ("default", "forced", "hearing_impaired"),
}

ADVANCED_DISPOSITIONS_BY_TYPE = {
    "video": (),
    "audio": ("dub", "original", "comment"),
    "subtitle": ("original", "comment", "dub", "captions", "descriptions"),
}

EDITABLE_DISPOSITIONS_BY_TYPE = {
    track_type: BASE_DISPOSITIONS_BY_TYPE[track_type] + ADVANCED_DISPOSITIONS_BY_TYPE[track_type]
    for track_type in TRACK_TYPE_CONFIG_KEYS
}

EXCLUDED_UI_DISPOSITIONS = frozenset(
    {
        "attached_pic",
        "timed_thumbnails",
        "metadata",
        "dependent",
        "still_image",
        "multilayer",
    }
)

LEGACY_DISPOSITION_KEYS = frozenset(
    {
        "default",
        "forced",
        "hearing_impaired",
        "visual_impaired",
        "comment",
    }
)


def is_ui_track_visible(track):
    disposition = getattr(track, "disposition", {}) or {}
    return not any(bool(disposition.get(name, 0)) for name in EXCLUDED_UI_DISPOSITIONS)


def iter_media_tracks(meta, track_type):
    if track_type == "video":
        return getattr(meta, "video_tracks", [])
    if track_type == "audio":
        return getattr(meta, "audio_tracks", [])
    return getattr(meta, "subtitle_tracks", [])


def build_default_track_settings(meta):
    settings = {}
    for track_type, config_key in TRACK_TYPE_CONFIG_KEYS.items():
        entries = []
        for position, track in enumerate(iter_media_tracks(meta, track_type), start=1):
            entries.append(build_track_entry(track_type, track=track, ui_id=str(position), keep=True))
        settings[config_key] = entries
    return settings


def build_track_entry(
    track_type,
    *,
    track=None,
    ui_id=None,
    original_index=None,
    codec_name="unknown",
    language="und",
    title="",
    keep=True,
    dispositions=None,
):
    normalized_dispositions = _empty_dispositions(track_type)

    if track is not None:
        original_index = getattr(track, "index", original_index)
        codec_name = getattr(track, "codec_name", codec_name)
        language = getattr(track, "language", language) or "und"
        title = getattr(track, "title", title) or ""
        source_dispositions = getattr(track, "disposition", {}) or {}
        for name in normalized_dispositions:
            normalized_dispositions[name] = bool(source_dispositions.get(name, 0))
    else:
        for name in normalized_dispositions:
            normalized_dispositions[name] = bool((dispositions or {}).get(name, False))

    if ui_id is None:
        ui_id = str(original_index if original_index is not None else "")

    return {
        "ui_id": str(ui_id),
        "original_index": int(original_index) if original_index is not None else -1,
        "codec_name": codec_name or "unknown",
        "language": language or "und",
        "title": title or "",
        "keep": bool(keep),
        "dispositions": normalized_dispositions,
    }


def normalize_track_settings(track_settings, meta=None):
    defaults = build_default_track_settings(meta) if meta is not None else _empty_track_settings()
    if not isinstance(track_settings, dict):
        return copy.deepcopy(defaults)

    normalized = copy.deepcopy(defaults)
    for config_key, track_type in CONFIG_KEY_TO_TRACK_TYPE.items():
        if config_key not in track_settings:
            normalized[config_key] = _ensure_default_exclusive(normalized.get(config_key, []))
            continue

        provided_entries = track_settings.get(config_key)
        if not isinstance(provided_entries, list):
            normalized[config_key] = _ensure_default_exclusive(normalized.get(config_key, []))
            continue

        if len(provided_entries) == 0:
            normalized[config_key] = _mark_all_entries_keep(normalized.get(config_key, []), False)
            continue

        if _looks_like_legacy_entries(provided_entries):
            normalized[config_key] = _normalize_legacy_entries(
                track_type, provided_entries, normalized.get(config_key, [])
            )
        else:
            normalized[config_key] = _normalize_new_entries(
                track_type, provided_entries, normalized.get(config_key, [])
            )

        normalized[config_key] = _ensure_default_exclusive(normalized[config_key])

    return normalized


def get_effective_track_settings(meta):
    return normalize_track_settings(getattr(meta, "track_settings", None), meta)


def get_kept_track_entries(track_settings, track_type):
    config_key = TRACK_TYPE_CONFIG_KEYS[track_type]
    entries = track_settings.get(config_key, []) if isinstance(track_settings, dict) else []
    return [entry for entry in entries if entry.get("keep", False)]


def _empty_track_settings():
    return {config_key: [] for config_key in TRACK_TYPE_CONFIG_KEYS.values()}


def _empty_dispositions(track_type):
    return {name: False for name in EDITABLE_DISPOSITIONS_BY_TYPE[track_type]}


def _mark_all_entries_keep(entries, keep_value):
    updated = copy.deepcopy(entries)
    for entry in updated:
        entry["keep"] = bool(keep_value)
    return updated


def _looks_like_legacy_entries(entries):
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "dispositions" not in entry:
            return True
    return False


def _normalize_legacy_entries(track_type, entries, default_entries):
    normalized_entries = []
    remaining_defaults = {
        entry["original_index"]: entry
        for entry in _mark_all_entries_keep(default_entries, False)
    }
    for position, entry in enumerate(entries, start=1):
        normalized_entry = _normalize_legacy_entry(track_type, entry, ui_id=str(position))
        original_index = normalized_entry["original_index"]
        default_entry = remaining_defaults.pop(original_index, None)
        if default_entry is None:
            normalized_entries.append(normalized_entry)
            continue

        default_entry.update(
            {
                "ui_id": normalized_entry["ui_id"] or default_entry.get("ui_id", str(position)),
                "codec_name": normalized_entry["codec_name"],
                "language": normalized_entry["language"],
                "title": normalized_entry["title"],
                "keep": normalized_entry["keep"],
                "dispositions": normalized_entry["dispositions"],
            }
        )
        normalized_entries.append(default_entry)

    for position, default_entry in enumerate(default_entries, start=len(normalized_entries) + 1):
        original_index = default_entry["original_index"]
        if original_index not in remaining_defaults:
            continue
        appended_entry = copy.deepcopy(remaining_defaults[original_index])
        appended_entry["ui_id"] = appended_entry.get("ui_id") or str(position)
        normalized_entries.append(appended_entry)

    return normalized_entries


def _normalize_new_entries(track_type, entries, default_entries):
    normalized_entries = []
    remaining_defaults = {entry["original_index"]: copy.deepcopy(entry) for entry in default_entries}
    next_ui_id = 1

    for entry in entries:
        normalized_entry = _normalize_new_entry(track_type, entry)
        original_index = normalized_entry["original_index"]
        default_entry = remaining_defaults.pop(original_index, None)
        if default_entry is None:
            if not normalized_entry["ui_id"]:
                normalized_entry["ui_id"] = str(next_ui_id)
            normalized_entries.append(normalized_entry)
            next_ui_id += 1
            continue

        default_entry.update(
            {
                "ui_id": normalized_entry["ui_id"] or default_entry.get("ui_id", str(next_ui_id)),
                "codec_name": normalized_entry["codec_name"],
                "language": normalized_entry["language"],
                "title": normalized_entry["title"],
                "keep": normalized_entry["keep"],
                "dispositions": normalized_entry["dispositions"],
            }
        )
        normalized_entries.append(default_entry)
        next_ui_id += 1

    for default_entry in default_entries:
        original_index = default_entry["original_index"]
        if original_index not in remaining_defaults:
            continue
        appended_entry = copy.deepcopy(remaining_defaults[original_index])
        appended_entry["ui_id"] = appended_entry.get("ui_id") or str(next_ui_id)
        normalized_entries.append(appended_entry)
        next_ui_id += 1

    return normalized_entries


def _normalize_legacy_entry(track_type, entry, ui_id=""):
    dispositions = _empty_dispositions(track_type)
    for legacy_key in LEGACY_DISPOSITION_KEYS:
        if legacy_key in dispositions:
            dispositions[legacy_key] = bool(entry.get(legacy_key, False))

    return build_track_entry(
        track_type,
        ui_id=entry.get("ui_id", ui_id),
        original_index=entry.get("original_index"),
        codec_name=entry.get("codec_name", "unknown"),
        language=entry.get("language", "und"),
        title=entry.get("title", ""),
        keep=entry.get("keep", True),
        dispositions=dispositions,
    )


def _normalize_new_entry(track_type, entry):
    dispositions = _empty_dispositions(track_type)
    provided_dispositions = entry.get("dispositions", {}) if isinstance(entry, dict) else {}
    for name in dispositions:
        dispositions[name] = bool(provided_dispositions.get(name, False))

    return build_track_entry(
        track_type,
        ui_id=entry.get("ui_id", ""),
        original_index=entry.get("original_index"),
        codec_name=entry.get("codec_name", "unknown"),
        language=entry.get("language", "und"),
        title=entry.get("title", ""),
        keep=entry.get("keep", True),
        dispositions=dispositions,
    )


def _ensure_default_exclusive(entries):
    normalized_entries = copy.deepcopy(entries)
    default_found = False
    for entry in normalized_entries:
        dispositions = entry.get("dispositions", {})
        if not dispositions.get("default", False):
            continue
        if not default_found:
            default_found = True
            continue
        dispositions["default"] = False
    return normalized_entries
