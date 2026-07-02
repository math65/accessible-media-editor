"""Lecture des cue sheets (fichiers .cue / cuesheet embarqué).

Un cue sheet décrit comment une grande image audio (FLAC/WAV/APE…) se découpe en
pistes. Ce module ne fait que **parser** et **résoudre l'image** ; le découpage
ffmpeg proprement dit est fait ailleurs (batch_manager + ConversionTask).

Périmètre : un seul fichier image par cue (`FILE` unique). Les cue à `FILE`
multiples sont signalés (`CueSheet.multi_file`) et laissés à l'appelant.
"""

import os
from dataclasses import dataclass, field

# Extensions d'images audio couramment référencées par un .cue (ffmpeg les décode),
# plus large que les entrées acceptées à l'UI (un .cue pointe souvent un .ape/.wv).
_AUDIO_EXTENSIONS = {
    '.flac', '.wav', '.ape', '.wv', '.tta', '.m4a', '.mp3', '.ogg', '.opus',
    '.wma', '.aiff', '.aif', '.aac', '.dsf', '.mpc',
}

# Un cadre CD = 1/75 s.
_FRAMES_PER_SECOND = 75


@dataclass
class CueTrack:
    number: int
    title: str = ""
    performer: str = ""
    start_ms: int = 0
    end_ms: "int | None" = None


@dataclass
class CueSheet:
    album: str = ""
    album_performer: str = ""
    genre: str = ""
    date: str = ""
    audio_ref: "str | None" = None
    tracks: list = field(default_factory=list)
    multi_file: bool = False


def _unquote(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _extract_file_name(rest):
    """`"image file.flac" WAVE` → `image file.flac` (ou retire le type final si pas de guillemets)."""
    rest = rest.strip()
    if rest.startswith('"'):
        end = rest.find('"', 1)
        if end != -1:
            return rest[1:end]
    parts = rest.rsplit(None, 1)  # retire le dernier token (WAVE/MP3/BINARY…)
    return parts[0] if len(parts) == 2 else rest


def _parse_index_time(token):
    """`MM:SS:FF` (FF = cadres, 75/s) → millisecondes."""
    bits = token.split(':')
    try:
        if len(bits) == 3:
            minutes, seconds, frames = (int(part) for part in bits)
        elif len(bits) == 2:
            minutes, seconds = (int(part) for part in bits)
            frames = 0
        else:
            return 0
    except ValueError:
        return 0
    return (minutes * 60 + seconds) * 1000 + round(frames * 1000 / _FRAMES_PER_SECOND)


def parse_cue_text(text):
    """Parse le texte d'un cue sheet en CueSheet (sans end_ms ; voir finalize_tracks)."""
    sheet = CueSheet()
    current = None
    file_count = 0

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()

        if upper.startswith('REM '):
            parts = line[4:].strip().split(None, 1)
            if len(parts) == 2:
                key = parts[0].upper()
                value = _unquote(parts[1])
                if key == 'GENRE':
                    sheet.genre = value
                elif key == 'DATE':
                    sheet.date = value
        elif upper.startswith('FILE '):
            file_count += 1
            if file_count == 1:
                sheet.audio_ref = _extract_file_name(line[5:])
            else:
                sheet.multi_file = True
        elif upper.startswith('TRACK '):
            parts = line.split()
            try:
                number = int(parts[1])
            except (IndexError, ValueError):
                number = len(sheet.tracks) + 1
            current = CueTrack(number=number)
            sheet.tracks.append(current)
        elif upper.startswith('TITLE '):
            value = _unquote(line[6:])
            if current is None:
                sheet.album = value
            else:
                current.title = value
        elif upper.startswith('PERFORMER '):
            value = _unquote(line[10:])
            if current is None:
                sheet.album_performer = value
            else:
                current.performer = value
        elif upper.startswith('INDEX ') and current is not None:
            parts = line.split()
            if len(parts) >= 3:
                try:
                    index_num = int(parts[1])
                except ValueError:
                    index_num = -1
                # INDEX 00 = pré-gap, INDEX 01 = vrai début ; 01 vient après et l'emporte.
                if index_num in (0, 1):
                    current.start_ms = _parse_index_time(parts[2])

    return sheet


def finalize_tracks(tracks, total_ms):
    """Calcule end_ms : début de la piste suivante ; dernière piste = total_ms."""
    for i, track in enumerate(tracks):
        if i + 1 < len(tracks):
            track.end_ms = tracks[i + 1].start_ms
        elif total_ms and total_ms > track.start_ms:
            track.end_ms = total_ms
        else:
            track.end_ms = None
    return tracks


def _read_text(path):
    for encoding in ('utf-8-sig', 'cp1252', 'latin-1'):
        try:
            with open(path, 'r', encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    with open(path, 'rb') as handle:
        return handle.read().decode('utf-8', errors='replace')


def load_cue_file(path):
    """Charge un fichier .cue (encodages Windows tolérés) → CueSheet."""
    return parse_cue_text(_read_text(path))


def cuesheet_from_chapters(chapters, album="", album_performer=""):
    """Convertit des chapitres ffprobe (bloc CUESHEET natif FLAC) en CueSheet."""
    sheet = CueSheet(album=album, album_performer=album_performer)
    for position, chapter in enumerate(chapters, start=1):
        try:
            start_ms = int(round(float(chapter.get('start_time', 0)) * 1000))
        except (TypeError, ValueError):
            start_ms = 0
        tags = chapter.get('tags') or {}
        sheet.tracks.append(CueTrack(number=position, title=str(tags.get('title', '')), start_ms=start_ms))
    return sheet


def _list_audio_files(directory):
    try:
        entries = os.listdir(directory)
    except OSError:
        return []
    found = []
    for name in entries:
        if os.path.splitext(name)[1].lower() in _AUDIO_EXTENSIONS:
            full = os.path.join(directory, name)
            if os.path.isfile(full):
                found.append(full)
    return found


def _find_audio_by_stem(directory, stem):
    stem_lower = stem.lower()
    for full in _list_audio_files(directory):
        if os.path.splitext(os.path.basename(full))[0].lower() == stem_lower:
            return full
    return None


def resolve_cue_audio(cue_path, audio_ref):
    """Trouve l'image audio référencée par le .cue, avec replis tolérants.

    1) référence directe (relative au dossier du .cue) ; 2) même basename, autre
    extension ; 3) même basename que le .cue ; 4) unique fichier audio du dossier.
    Retourne le chemin absolu, ou None si introuvable/ambigu."""
    base_dir = os.path.dirname(os.path.abspath(cue_path))

    if audio_ref:
        candidate = audio_ref if os.path.isabs(audio_ref) else os.path.join(base_dir, audio_ref)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        match = _find_audio_by_stem(base_dir, os.path.splitext(os.path.basename(audio_ref))[0])
        if match:
            return os.path.abspath(match)

    match = _find_audio_by_stem(base_dir, os.path.splitext(os.path.basename(cue_path))[0])
    if match:
        return os.path.abspath(match)

    audio_files = _list_audio_files(base_dir)
    if len(audio_files) == 1:
        return os.path.abspath(audio_files[0])
    return None
