"""Parse season/episode info from a media file name.

This is the network-free automation every TV tagger relies on: the SxxExx token
in a filename is the industry's source of truth for which episode a file is.
Pure logic, no FFmpeg, no I/O beyond reading the given string.
"""

import os
import re

# Ordered most-specific first. Searched case-insensitively over the file stem.
# - S01E02 / s1e2 / S01.E02 / S01_E02  (also matches the first of S01E02E03)
# - Season 1 Episode 2
# - 1x02 / 01x02  (most ambiguous, kept last; guarded against 1280x720)
_PATTERNS = (
    re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})"),
    re.compile(r"[sS]eason[ ._-]*(\d{1,2})[ ._-]*[eE]pisode[ ._-]*(\d{1,3})"),
    re.compile(r"(?<![\dxX])(\d{1,2})[xX](\d{1,3})(?!\d)"),
)


def _clean(text):
    """Turn scene-style separators into spaces and trim noise."""
    text = re.sub(r"[._]+", " ", text or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -")


def parse_episode_from_filename(filename):
    """Return {season, episode, show, episode_title} or None if no match.

    ``season``/``episode`` are ints; ``show``/``episode_title`` are best-effort
    strings (or None). The title side is often noisy (release tags), so callers
    should treat it as a hint, not authoritative.
    """
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    for pattern in _PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        return {
            "season": int(match.group(1)),
            "episode": int(match.group(2)),
            "show": _clean(stem[: match.start()]) or None,
            "episode_title": _clean(stem[match.end():]) or None,
        }
    return None


# Track-number patterns for music files, conservative to avoid matching a year
# (capped at 2 digits) or unrelated numbers. Most specific first:
# - "1-02" / "1.02"  disc-track at the very start
# - " - 03 - "       a number delimited by spaced hyphens (Artist - NN - Title)
# - "01 ", "01. ", "01_", "01-" leading track number
_TRACK_PATTERNS = (
    re.compile(r"^\s*(?P<disc>\d{1,2})[-.](?P<track>\d{1,2})(?=[\s._\-]|$)"),
    re.compile(r"\s-\s(?P<track>\d{1,2})\s-\s"),
    re.compile(r"^\s*(?P<track>\d{1,2})(?=[\s._\-]|$)"),
)


def parse_track_from_filename(filename):
    """Return {track, disc} parsed from a music file name, or None.

    ``track`` is an int; ``disc`` is an int or None. Deliberately conservative:
    it only fires on a track number at the start of the name (optionally a
    disc-track pair) or one clearly delimited by ' - ', so a stray number in the
    title is not mistaken for a track.
    """
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0]
    for pattern in _TRACK_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue
        groups = match.groupdict()
        return {
            "track": int(groups["track"]),
            "disc": int(groups["disc"]) if groups.get("disc") else None,
        }
    return None
