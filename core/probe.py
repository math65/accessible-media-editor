import builtins
import json
import logging
import os
import subprocess

from core.cue import finalize_tracks, load_cue_file, resolve_cue_audio
from core.ffmpeg_helpers import get_ffprobe_path
from core.track_settings import is_ui_track_visible

FFPROBE_TIMEOUT_SECONDS = 30


def _translate(msgid):
    translator = builtins.__dict__.get('_')
    if callable(translator):
        return translator(msgid)
    return msgid


def _translatef(msgid, **kwargs):
    return _translate(msgid).format(**kwargs)


def _parse_frame_rate(stream):
    """Frames-per-second d'un flux vidéo (float), ou 0.0 si indisponible.

    Préfère avg_frame_rate (moyenne réelle) à r_frame_rate (base nominale, qui
    peut être un énorme tbr en VFR). ffprobe émet une fraction 'num/den'. Sert à
    convertir un timecode image 'HH:MM:SS:FF' en millisecondes côté éditeur."""
    for key in ('avg_frame_rate', 'r_frame_rate'):
        val = stream.get(key)
        if not val or str(val) in ('0/0', '0'):
            continue
        try:
            text = str(val)
            if '/' in text:
                num_str, den_str = text.split('/', 1)
                num, den = float(num_str), float(den_str)
                if den:
                    return num / den
            else:
                return float(text)
        except (ValueError, TypeError):
            continue
    return 0.0

class MediaTrack:
    def __init__(self, stream_index, codec_type, codec_name, language='und', title=None, disposition=None):
        self.index = stream_index
        self.codec_type = codec_type
        self.codec_name = codec_name
        self.language = language
        self.title = title
        self.disposition = disposition if disposition else {}

    def is_default(self): return self.disposition.get('default', 0) == 1
    def is_forced(self): return self.disposition.get('forced', 0) == 1
    def is_attached_pic(self): return self.disposition.get('attached_pic', 0) == 1
    def is_hidden_from_ui(self): return not is_ui_track_visible(self)

    def get_summary(self):
        parts = [self.codec_name.upper()]
        if self.language and self.language != 'und':
            parts.append(self.language.upper())
        if self.title:
            parts.append(f"\"{self.title}\"")
        return " - ".join(parts)

class MediaMetadata:
    def __init__(self, path):
        self.full_path = path
        self.filename = os.path.basename(path)
        # Sous-dossier relatif à la racine ajoutée (vide si fichier ajouté seul).
        # Sert à recréer l'arborescence d'origine sous un dossier de sortie
        # personnalisé quand la préférence preserve_folder_structure est active.
        self.relative_dir = ""
        self.duration = 0
        self.size_bytes = 0
        self.video_tracks = []
        self.audio_tracks = []
        self.subtitle_tracks = []
        self.video_codec = ""
        self.audio_codec = ""
        self.width = 0
        self.height = 0
        # Cadence du flux vidéo (images/s). Sert à interpréter une saisie de
        # timecode image 'HH:MM:SS:FF' (dernier champ = numéro d'image) dans
        # l'éditeur de segments. 0.0 = inconnu (audio seul, VFR non résolu…).
        self.video_fps = 0.0
        self.has_video = False
        self.is_image = False
        self.track_settings = None
        self.audio_extract_track = None
        self.format_tags = {}
        self.has_cover_art = False
        self.metadata_overrides = None
        # Override de sortie par fichier : {"format": fmt_key, "settings": {...}}.
        # Quand présent, ce fichier est converti avec ce format/qualité au lieu du global.
        self.output_override = None
        # Découpage cue : si cue_sheet (core.cue.CueSheet) est posé, ce media est une
        # image album à découper en N pistes. has_embedded_cue signale un cuesheet
        # intégré (FLAC) que l'utilisateur peut activer ; cue_error = message d'ajout.
        self.cue_sheet = None
        self.has_embedded_cue = False
        self.embedded_cue_text = None
        self.embedded_chapters = None
        self.cue_error = None
        self.source_format_name = ""
        # Découpage manuel : core.segments.SegmentPlan posé par l'éditeur de
        # segments (Cut / Split). Quand présent, ce media est découpé en N sorties
        # (une par région gardée) via batch_manager, ou reconcaténé en 1 fichier
        # (SegmentExportTask) selon le mode d'export choisi.
        self.segment_plan = None

    @property
    def has_audio(self): return len(self.audio_tracks) > 0
    @property
    def has_subtitles(self): return len(self.subtitle_tracks) > 0

    def get_audio_track_by_index(self, original_index):
        for track in self.audio_tracks:
            if track.index == original_index:
                return track
        return None

    def get_default_audio_track(self):
        for track in self.audio_tracks:
            if track.is_default():
                return track
        if self.audio_tracks:
            return self.audio_tracks[0]
        return None

    def get_preferred_audio_track(self, preferred_index=None):
        if preferred_index is not None:
            preferred_track = self.get_audio_track_by_index(preferred_index)
            if preferred_track is not None:
                return preferred_track
        return self.get_default_audio_track()

    def get_summary(self):
        if self.is_image:
            parts = []
            if self.width and self.height:
                parts.append(f"{self.width}x{self.height}")
            if self.video_codec:
                parts.append(self.video_codec.upper())
            return " / ".join(parts) if parts else _translate("Image")

        v_info = ""
        if self.video_tracks:
            v = self.video_tracks[0]
            v_info = f"{v.codec_name.upper()}"
            if self.width and self.height:
                v_info += f" ({self.width}x{self.height})"

        a_info = ""
        count_a = len(self.audio_tracks)
        if count_a > 0:
            a = self.audio_tracks[0]
            if count_a > 1:
                a_info = _translatef("{count}x Audio", count=count_a)
            else:
                a_info = a.codec_name.upper()

        s_info = ""
        count_s = len(self.subtitle_tracks)
        if count_s > 0:
            s_info = _translatef("{count}x Subtitles", count=count_s)

        parts = [x for x in [v_info, a_info, s_info] if x]
        return " / ".join(parts)

class FileProber:
    def __init__(self):
        pass

    def analyze(self, file_path):
        meta = MediaMetadata(file_path)

        if not os.path.exists(file_path):
            logging.error("Fichier introuvable : %s", file_path)
            return meta

        meta.size_bytes = os.path.getsize(file_path)

        if os.path.splitext(file_path)[1].lower() == '.cue':
            return self._analyze_cue(meta, file_path)

        ffprobe = get_ffprobe_path()

        cmd = [
            ffprobe,
            '-v', 'quiet',
            '-print_format', 'json',
            '-show_format',
            '-show_streams',
            '-show_chapters',
            file_path,
        ]

        try:
            output = subprocess.check_output(
                cmd,
                startupinfo=self._get_startup_info(),
                timeout=FFPROBE_TIMEOUT_SECONDS,
            )
            data = json.loads(output)

            fmt = data.get('format', {})
            try:
                meta.duration = float(fmt.get('duration', 0))
            except (TypeError, ValueError):
                meta.duration = 0

            meta.source_format_name = str(fmt.get('format_name', '') or '')
            format_tags = fmt.get('tags', {})
            if isinstance(format_tags, dict):
                meta.format_tags = {
                    str(key).lower(): value for key, value in format_tags.items()
                }

            for stream in data.get('streams', []):
                idx = stream.get('index')
                c_type = stream.get('codec_type')
                c_name = stream.get('codec_name', 'unknown')
                tags = stream.get('tags', {})
                lang = tags.get('language', 'und')
                title = tags.get('title', None)
                disposition = stream.get('disposition', {})

                track = MediaTrack(idx, c_type, c_name, lang, title, disposition)

                if c_type == 'video' and disposition.get('attached_pic', 0) == 1:
                    meta.has_cover_art = True

                if c_type == 'video':
                    if not track.is_hidden_from_ui():
                        meta.video_tracks.append(track)
                        meta.has_video = True
                        if meta.width == 0:
                            meta.width = stream.get('width', 0)
                            meta.height = stream.get('height', 0)
                            meta.video_codec = c_name
                            meta.video_fps = _parse_frame_rate(stream)

                elif c_type == 'audio':
                    if not track.is_hidden_from_ui():
                        meta.audio_tracks.append(track)
                        if not meta.audio_codec:
                            meta.audio_codec = c_name

                elif c_type == 'subtitle':
                    if not track.is_hidden_from_ui():
                        meta.subtitle_tracks.append(track)

            meta.is_image = self._detect_image(meta, fmt)
            if meta.is_image:
                meta.has_video = False
            else:
                self._detect_embedded_cue(meta, data)

        except subprocess.TimeoutExpired:
            logging.error("ffprobe timeout (%ss) on %s", FFPROBE_TIMEOUT_SECONDS, file_path)
        except Exception:
            logging.exception("Erreur fatale probing %s", file_path)

        return meta

    def _analyze_cue(self, meta, cue_path):
        """Sonde un fichier .cue : parse le cue, résout l'image audio et la sonde
        pour la durée totale, puis attache le CueSheet (toujours, même en erreur,
        pour que la ligne soit reconnue comme une ligne album)."""
        try:
            sheet = load_cue_file(cue_path)
        except Exception:
            logging.exception("Échec du parsing du cue : %s", cue_path)
            meta.cue_error = _translate("This cue sheet could not be read.")
            return meta

        meta.cue_sheet = sheet

        if sheet.multi_file:
            meta.cue_error = _translate("Cue sheets referencing multiple files are not supported yet.")
            return meta
        if not sheet.tracks:
            meta.cue_error = _translate("This cue sheet contains no tracks.")
            return meta

        audio_path = resolve_cue_audio(cue_path, sheet.audio_ref)
        if not audio_path:
            meta.cue_error = _translatef(
                "Audio file referenced by the cue sheet not found: {name}",
                name=sheet.audio_ref or "?",
            )
            return meta

        # Sonde l'image audio réelle (réutilise le chemin ffprobe normal) pour la
        # durée totale et le codec ; on garde full_path = le .cue pour l'affichage.
        audio_meta = self.analyze(audio_path)
        meta.duration = audio_meta.duration
        meta.audio_codec = audio_meta.audio_codec
        meta.audio_tracks = audio_meta.audio_tracks
        meta.source_format_name = audio_meta.source_format_name

        sheet.audio_ref = audio_path  # chemin absolu résolu, consommé par le batch
        finalize_tracks(sheet.tracks, int(round((meta.duration or 0) * 1000)))
        return meta

    def _detect_embedded_cue(self, meta, data):
        """Signale un cue sheet embarqué : tag CUESHEET (texte, EAC/foobar) ou
        chapitres ffprobe (bloc CUESHEET natif FLAC). Découpage opt-in côté UI."""
        cue_text = meta.format_tags.get('cuesheet')
        if isinstance(cue_text, str) and 'TRACK' in cue_text.upper():
            meta.has_embedded_cue = True
            meta.embedded_cue_text = cue_text
            return

        chapters = data.get('chapters') or []
        if len(chapters) > 1:
            meta.has_embedded_cue = True
            meta.embedded_chapters = chapters

    def _detect_image(self, meta, fmt_data):
        IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.tiff', '.tif', '.bmp', '.heic', '.heif'}
        IMAGE_FORMAT_NAMES = {'image2', 'jpeg_pipe', 'png_pipe', 'webp_pipe', 'bmp_pipe', 'tiff_pipe', 'avif'}
        ext = os.path.splitext(meta.filename)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            return True
        format_name = str(fmt_data.get('format_name', '')).lower()
        if any(img_fmt in format_name for img_fmt in IMAGE_FORMAT_NAMES):
            return True
        if meta.video_tracks and not meta.audio_tracks and meta.duration <= 0:
            return True
        return False

    def _get_startup_info(self):
        if os.name == 'nt':
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            return info
        return None
