"""Pure logic for editing file-level metadata (tags + cover art).

Shared by the conversion path (``core/conversion.py``) and the in-place
re-tag path (``core/metadata_retag.py``). No gettext at module level: the
labels are msgids consumed by the UI, which calls ``_()`` itself.
"""

from core.episode_parse import parse_episode_from_filename, parse_track_from_filename


def N_(message):
    """gettext no-op: marks a literal for extraction without translating it.

    The UI translates these label msgids at render time via ``_()``; we only
    need them collected into the catalog. Same pattern as ``core/support.py``.
    """
    return message


# Field sets are (ffmpeg metadata key, UI label msgid) in display / command order.
#
# Audio keeps the music-oriented tags. Video splits by *content type* (film /
# series / other): an album/track layout makes no sense for a movie. Every video
# key below was verified to round-trip in both MP4 and MKV, for re-encode and
# stream-copy alike (season/episode are integer atoms in MP4, hence numeric).
# director/studio/producer are intentionally excluded: they have no standard MP4
# atom and are silently dropped on MP4 output.

AUDIO_METADATA_FIELDS = (
    ("title", N_("Title")),
    ("artist", N_("Artist")),
    ("album", N_("Album")),
    ("album_artist", N_("Album artist")),
    ("composer", N_("Composer")),
    ("date", N_("Year")),
    ("track", N_("Track number")),
    ("disc", N_("Disc number")),
    ("genre", N_("Genre")),
    ("comment", N_("Comment")),
    ("grouping", N_("Grouping")),
    ("copyright", N_("Copyright")),
    ("lyrics", N_("Lyrics")),
)

# Album edited as a batch with per-file track auto-detection on: only the fields
# genuinely shared across the album are shown. Title / track / disc (and lyrics)
# are per-track and come from each file (track/disc from its name), so omitted.
AUDIO_BATCH_FIELDS = (
    ("artist", N_("Artist")),
    ("album", N_("Album")),
    ("album_artist", N_("Album artist")),
    ("composer", N_("Composer")),
    ("date", N_("Year")),
    ("genre", N_("Genre")),
    ("comment", N_("Comment")),
    ("grouping", N_("Grouping")),
    ("copyright", N_("Copyright")),
)

# Fields rendered as a multi-line text box (long free text).
MULTILINE_TAG_KEYS = frozenset({"lyrics"})

VIDEO_FILM_FIELDS = (
    ("title", N_("Title")),
    ("date", N_("Year")),
    ("genre", N_("Genre")),
    ("description", N_("Synopsis")),
    ("comment", N_("Comment")),
)

VIDEO_SERIES_FIELDS = (
    ("title", N_("Episode title")),
    ("show", N_("Series")),
    ("season_number", N_("Season")),
    ("episode_id", N_("Episode")),
    ("date", N_("Year")),
    ("genre", N_("Genre")),
    ("description", N_("Synopsis")),
    ("comment", N_("Comment")),
)

VIDEO_OTHER_FIELDS = (
    ("title", N_("Title")),
    ("date", N_("Year")),
    ("genre", N_("Genre")),
    ("comment", N_("Comment")),
)

# Series, edited as a batch with per-file episode auto-detection on: only the
# fields that are genuinely shared across the season are shown. Season/episode
# (and the episode title) are unique per file and come from each file name, so
# they are omitted here and injected at apply time.
VIDEO_SERIES_BATCH_FIELDS = (
    ("show", N_("Series")),
    ("date", N_("Year")),
    ("genre", N_("Genre")),
    ("comment", N_("Comment")),
)

# Keys FFmpeg stores as integer atoms in MP4 (e.g. tvsn for the season): a
# non-numeric value is silently coerced to 0, so the editor rejects it up front.
NUMERIC_TAG_KEYS = frozenset({"season_number"})

CONTENT_TYPE_FILM = "film"
CONTENT_TYPE_SERIES = "series"
CONTENT_TYPE_OTHER = "other"

# (content_type id, selector label msgid, field set) in selector display order.
VIDEO_CONTENT_TYPES = (
    (CONTENT_TYPE_FILM, N_("Film"), VIDEO_FILM_FIELDS),
    (CONTENT_TYPE_SERIES, N_("TV series"), VIDEO_SERIES_FIELDS),
    (CONTENT_TYPE_OTHER, N_("Other"), VIDEO_OTHER_FIELDS),
)

_VIDEO_FIELDS_BY_TYPE = {ctype: fields for ctype, _label, fields in VIDEO_CONTENT_TYPES}


def _ordered_union(*field_groups):
    """Merge field groups keeping first-seen order, deduplicated by key."""
    merged = {}
    for group in field_groups:
        for key, label in group:
            merged.setdefault(key, label)
    return tuple(merged.items())


# Superset of every key any set can produce. Drives METADATA_TAG_KEYS, which
# read_prefill_tags / build_tag_metadata_args / normalize_metadata_overrides use.
# Those only ever emit keys actually present in a tag dict, so widening the
# superset is safe for the audio and conversion paths.
METADATA_TAG_FIELDS = _ordered_union(
    AUDIO_METADATA_FIELDS, VIDEO_FILM_FIELDS, VIDEO_SERIES_FIELDS, VIDEO_OTHER_FIELDS
)

METADATA_TAG_KEYS = tuple(key for key, _label in METADATA_TAG_FIELDS)


def fields_for_content_type(content_type):
    """Ordered (key, label) field set for a video content type (default: film)."""
    return _VIDEO_FIELDS_BY_TYPE.get(content_type, VIDEO_FILM_FIELDS)


def detect_content_type(meta):
    """Best-effort default content type for a video file.

    Series when the source already carries show/season/episode tags, or when the
    file name has a recognizable SxxExx / NxNN token; film otherwise.
    """
    tags = getattr(meta, "format_tags", {}) or {}
    lowered = {str(key).lower() for key in tags}
    series_markers = {
        "show", "tvshow", "season_number", "season",
        "episode_id", "episode_sort", "episode",
    }
    if lowered & series_markers:
        return CONTENT_TYPE_SERIES

    name = getattr(meta, "full_path", "") or getattr(meta, "filename", "")
    if parse_episode_from_filename(name):
        return CONTENT_TYPE_SERIES
    return CONTENT_TYPE_FILM

# ffprobe tag aliases -> our canonical field key (keys are lowercased on read).
_TAG_ALIASES = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "album_artist": "album_artist",
    "albumartist": "album_artist",
    "album artist": "album_artist",
    "composer": "composer",
    "date": "date",
    "year": "date",
    "track": "track",
    "tracknumber": "track",
    "disc": "disc",
    "discnumber": "disc",
    "genre": "genre",
    "comment": "comment",
    # Video / film / series.
    "description": "description",
    "synopsis": "description",
    "show": "show",
    "tvshow": "show",
    "season_number": "season_number",
    "season": "season_number",
    "episode_id": "episode_id",
    "episode": "episode_id",
    "episode_sort": "episode_id",
    # Extra music tags.
    "grouping": "grouping",
    "copyright": "copyright",
    "lyrics": "lyrics",
    "unsyncedlyrics": "lyrics",
    "unsynced lyrics": "lyrics",
}

# Output audio formats whose container can embed a cover (attached_pic) image.
# Cover embedding during *conversion* is limited to audio targets: mixing a new
# cover with a re-encoded video stream in a container is intentionally deferred.
COVER_CAPABLE_AUDIO = ("mp3", "aac", "alac", "flac")
COVER_CAPABLE_FORMATS = COVER_CAPABLE_AUDIO

COVER_STREAM_TITLE = "Album cover"

VALID_COVER_ACTIONS = ("keep", "replace", "remove")


def format_supports_cover(format_key):
    return str(format_key or "").lower() in COVER_CAPABLE_FORMATS


def source_supports_cover(format_name):
    """True if a source container (ffprobe format_name) can embed a cover.

    Used by the in-place re-tag path where the target is the source format.
    """
    name = str(format_name or "").lower()
    if not name:
        return False
    tokens = {token.strip() for token in name.split(",")}
    cover_tokens = {"mp3", "mov", "mp4", "m4a", "ipod", "flac", "matroska", "matroska,webm"}
    return bool(tokens & cover_tokens) or "mp4" in name or "matroska" in name


def read_prefill_tags(format_tags):
    """Map ffprobe format tags to our editor field keys (single-file prefill)."""
    prefilled = {key: "" for key in METADATA_TAG_KEYS}
    if not isinstance(format_tags, dict):
        return prefilled

    for raw_key, value in format_tags.items():
        canonical = _TAG_ALIASES.get(str(raw_key).lower())
        if canonical and not prefilled.get(canonical):
            prefilled[canonical] = "" if value is None else str(value)
    return prefilled


def normalize_metadata_overrides(overrides):
    """Return {} when nothing to apply, else a clean
    {"tags": {key: value, ...}, "cover": {"action": ..., "path": ...}} dict.

    ``tags`` keeps only known keys, in field order. ``cover`` defaults to keep.
    """
    if not isinstance(overrides, dict):
        return {}

    raw_tags = overrides.get("tags", {})
    tags = {}
    if isinstance(raw_tags, dict):
        for key in METADATA_TAG_KEYS:
            if key in raw_tags and raw_tags[key] is not None:
                tags[key] = str(raw_tags[key])

    raw_cover = overrides.get("cover", {})
    action = "keep"
    path = ""
    if isinstance(raw_cover, dict):
        candidate = str(raw_cover.get("action", "keep") or "keep").lower()
        if candidate in VALID_COVER_ACTIONS:
            action = candidate
        path = str(raw_cover.get("path", "") or "")

    if action == "replace" and not path:
        action = "keep"

    cover = {"action": action}
    if action == "replace":
        cover["path"] = path

    normalized = {"tags": tags, "cover": cover}
    if not overrides_are_effective(normalized):
        return {}
    return normalized


def overrides_are_effective(overrides):
    if not isinstance(overrides, dict):
        return False
    if overrides.get("tags"):
        return True
    cover = overrides.get("cover", {})
    return isinstance(cover, dict) and cover.get("action", "keep") != "keep"


def has_metadata_overrides(meta):
    return overrides_are_effective(getattr(meta, "metadata_overrides", None))


def get_metadata_overrides(meta):
    return normalize_metadata_overrides(getattr(meta, "metadata_overrides", None))


def overrides_with_detected_episode(base_overrides, filename):
    """Merge season/episode parsed from ``filename`` over ``base_overrides``.

    Used for batch series tagging: shared fields (series, year, genre…) come
    from the dialog and are identical for every file, while season and episode
    are unique and read from each file's own name. Returns ``base_overrides``
    unchanged when the name has no recognizable SxxExx / NxNN token.
    """
    parsed = parse_episode_from_filename(filename)
    if not parsed:
        return base_overrides

    base = base_overrides if isinstance(base_overrides, dict) else {}
    tags = dict(base.get("tags", {}))
    tags["season_number"] = str(parsed["season"])
    tags["episode_id"] = str(parsed["episode"])
    merged = {"tags": tags, "cover": base.get("cover", {"action": "keep"})}
    return normalize_metadata_overrides(merged)


def overrides_with_detected_track(base_overrides, filename):
    """Merge track (and disc) parsed from ``filename`` over ``base_overrides``.

    The audio analogue of ``overrides_with_detected_episode``: for batch album
    tagging the shared fields (album, artist, year…) come from the dialog while
    each file's track number is read from its own name.
    """
    parsed = parse_track_from_filename(filename)
    if not parsed:
        return base_overrides

    base = base_overrides if isinstance(base_overrides, dict) else {}
    tags = dict(base.get("tags", {}))
    tags["track"] = str(parsed["track"])
    if parsed.get("disc"):
        tags["disc"] = str(parsed["disc"])
    merged = {"tags": tags, "cover": base.get("cover", {"action": "keep"})}
    return normalize_metadata_overrides(merged)


def overrides_with_detected_numbers(base_overrides, filename, kind):
    """Dispatch per-file number detection by kind ('episode' / 'track')."""
    if kind == "episode":
        return overrides_with_detected_episode(base_overrides, filename)
    if kind == "track":
        return overrides_with_detected_track(base_overrides, filename)
    return base_overrides


def build_tag_metadata_args(tags):
    """Build ['-metadata', 'key=value', ...] in field order. Empty value clears."""
    args = []
    if not isinstance(tags, dict):
        return args
    for key in METADATA_TAG_KEYS:
        if key in tags:
            args.extend(["-metadata", f"{key}={tags[key]}"])
    return args


def cover_stream_args(output_video_index=0):
    """Disposition + title for the cover stream at output video index N.

    Caller is responsible for ``-i <cover>``, the ``-map`` directives and
    ``-c:v copy`` (the surrounding command differs between audio and video).
    """
    spec = f"v:{output_video_index}"
    return [
        f"-disposition:{spec}",
        "attached_pic",
        f"-metadata:s:{spec}",
        f"title={COVER_STREAM_TITLE}",
    ]
