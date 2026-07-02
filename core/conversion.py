import builtins
import logging
import os
import re
import subprocess

from core.ffmpeg_helpers import (
    COVER_ART_AUDIO_OUTPUTS,
    STREAMING_LOUDNORM_FILTER,
    VIDEO_CONTAINER_OUTPUTS,
    apply_audio_codec_args,
    apply_common_audio_options,
    apply_metadata_preservation,
    apply_video_codec_args,
    get_ffmpeg_path,
    get_ffprobe_path,
    is_transport_stream,
    parse_ffmpeg_threads,
    resolve_audio_codec_key,
)
from core.formatting import IMAGE_OUTPUT_FORMAT_KEYS
from core.metadata_edit import (
    build_tag_metadata_args,
    cover_stream_args,
    get_metadata_overrides,
    overrides_are_effective,
)
from core.track_settings import get_effective_track_settings, get_kept_track_entries


def _translate(msgid):
    translator = builtins.__dict__.get('_')
    if callable(translator):
        return translator(msgid)
    return msgid


def _translatef(msgid, **kwargs):
    return _translate(msgid).format(**kwargs)


MP4_TEXT_SUBTITLE_CODECS = frozenset(
    {
        "subrip",
        "srt",
        "ass",
        "ssa",
        "webvtt",
        "text",
        "mov_text",
    }
)


def get_output_extension(target_format):
    if target_format in ['alac', 'aac']:
        return 'm4a'
    if target_format == 'jpeg':
        return 'jpg'
    if target_format == 'tiff':
        return 'tif'
    return target_format


def build_output_filename(input_path, target_format):
    extension = get_output_extension(target_format)
    base_name = os.path.splitext(os.path.basename(input_path))[0]
    return f"{base_name}.{extension}"


def resolve_output_dir(input_path, custom_output_dir=None):
    if custom_output_dir and os.path.isdir(custom_output_dir):
        return custom_output_dir
    return os.path.dirname(input_path) or os.getcwd()


def build_output_path(input_path, target_format, custom_output_dir=None, relative_dir=""):
    output_dir = resolve_output_dir(input_path, custom_output_dir=custom_output_dir)
    # Recrée l'arborescence d'origine uniquement vers un dossier de sortie
    # personnalisé ; en mode « source » la structure est déjà préservée.
    if relative_dir and custom_output_dir and os.path.isdir(custom_output_dir):
        output_dir = os.path.join(output_dir, relative_dir)
    return os.path.join(output_dir, build_output_filename(input_path, target_format))


def _format_ffmpeg_time(ms):
    """Millisecondes → 'HH:MM:SS.mmm' accepté par ffmpeg (-ss/-t)."""
    total_seconds = max(0, ms) / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def sanitize_filename(name):
    """Retire les caractères interdits dans un nom de fichier Windows."""
    cleaned = re.sub(r'[\\/:*?"<>|]', '_', str(name)).strip(' .')
    return cleaned or "_"


def build_cue_track_output_path(image_path, album, number, total, title, target_format,
                                custom_output_dir=None, relative_dir=""):
    """Chemin d'une piste découpée : <dossier>/<album>/NN - Titre.ext."""
    base_dir = resolve_output_dir(image_path, custom_output_dir=custom_output_dir)
    if relative_dir and custom_output_dir and os.path.isdir(custom_output_dir):
        base_dir = os.path.join(base_dir, relative_dir)
    album_dir = os.path.join(base_dir, sanitize_filename(album) if album else "Album")
    width = max(2, len(str(total)))
    extension = get_output_extension(target_format)
    safe_title = sanitize_filename(title) if title else _translate("Track {number}").format(number=number)
    return os.path.join(album_dir, f"{number:0{width}d} - {safe_title}.{extension}")


def build_segment_output_path(input_path, number, total, label, target_format,
                              custom_output_dir=None, relative_dir=""):
    """Chemin d'un segment découpé manuellement : <dossier>/<nom> - <label|part NN>.ext.

    Contrairement au split de cue, pas de sous-dossier album : les morceaux d'un
    même fichier restent à côté de la source (ou dans le dossier de sortie choisi).
    """
    base_dir = resolve_output_dir(input_path, custom_output_dir=custom_output_dir)
    if relative_dir and custom_output_dir and os.path.isdir(custom_output_dir):
        base_dir = os.path.join(base_dir, relative_dir)
    stem = sanitize_filename(os.path.splitext(os.path.basename(input_path))[0])
    extension = get_output_extension(target_format)
    if label:
        suffix = sanitize_filename(label)
    else:
        width = max(2, len(str(total)))
        suffix = _translate("part {number}").format(number=f"{number:0{width}d}")
    return os.path.join(base_dir, f"{stem} - {suffix}.{extension}")


class ConversionTask:
    def __init__(self, input_data, target_format, settings, output_dir=None, output_path=None,
                 clip=None, extra_tags=None, input_path_override=None):
        self.meta = None
        if hasattr(input_data, 'full_path'):
            self.meta = input_data
            self.input_path = input_data.full_path
            self.duration = float(input_data.duration)
        else:
            self.input_path = str(input_data)
            self.duration = 0.0

        # Mode « clip » (découpage cue) : on lit une tranche [start, end] d'une image
        # audio via input_path_override et on applique des tags explicites par piste.
        self.clip = clip
        self.extra_tags = extra_tags
        if input_path_override:
            self.input_path = input_path_override
        if clip:
            start_ms, end_ms = clip
            if end_ms is not None and end_ms > start_ms:
                self.duration = (end_ms - start_ms) / 1000.0

        self.target_format = target_format
        self.settings = settings
        self.custom_output_dir = output_dir
        self.output_path = output_path
        self.ffmpeg_exe = get_ffmpeg_path()
        self.ffprobe_exe = get_ffprobe_path()
        self.process = None
        self.last_command = []
        self.stderr_lines = []

        logging.debug("Tâche initialisée : %s -> %s", self.input_path, self.target_format)

    def _is_video_to_audio_conversion(self):
        return bool(
            self.meta
            and getattr(self.meta, 'has_video', False)
            and self.target_format not in VIDEO_CONTAINER_OUTPUTS
        )

    def _find_audio_track_by_index(self, original_index):
        if self.meta is None:
            return None

        if hasattr(self.meta, 'get_audio_track_by_index'):
            return self.meta.get_audio_track_by_index(original_index)

        for track in getattr(self.meta, 'audio_tracks', []):
            if getattr(track, 'index', None) == original_index:
                return track
        return None

    def _get_default_audio_track(self):
        if self.meta is None:
            return None

        if hasattr(self.meta, 'get_default_audio_track'):
            return self.meta.get_default_audio_track()

        audio_tracks = getattr(self.meta, 'audio_tracks', [])
        for track in audio_tracks:
            if hasattr(track, 'is_default') and track.is_default():
                return track
        if audio_tracks:
            return audio_tracks[0]
        return None

    def _resolve_audio_extract_track(self):
        selected_track_data = getattr(self.meta, 'audio_extract_track', None) if self.meta else None
        if isinstance(selected_track_data, dict):
            original_index = selected_track_data.get('original_index')
            selected_track = self._find_audio_track_by_index(original_index)
            if selected_track is not None:
                return selected_track, "manual"

            logging.warning(
                "La piste audio d'extraction sélectionnée n'existe plus (stream #%s). Fallback automatique.",
                original_index,
            )

        default_track = self._get_default_audio_track()
        if default_track is None:
            return None, "missing"

        if hasattr(default_track, 'is_default') and default_track.is_default():
            return default_track, "default"
        return default_track, "first"

    def _apply_audio_track_metadata(self, cmd, track):
        if track.language and track.language != 'und':
            cmd.extend(["-metadata:s:a:0", f"language={track.language}"])
        if track.title:
            cmd.extend(["-metadata:s:a:0", f"title={track.title}"])

    def _apply_track_entry_metadata(self, cmd, track_type, output_index, track_entry):
        stream_letter = {"video": "v", "audio": "a", "subtitle": "s"}[track_type]
        language = track_entry.get("language")
        title = track_entry.get("title")

        if language and language != "und":
            cmd.extend([f"-metadata:s:{stream_letter}:{output_index}", f"language={language}"])
        if title:
            cmd.extend([f"-metadata:s:{stream_letter}:{output_index}", f"title={title}"])

        active_dispositions = [
            disposition_name
            for disposition_name, enabled in track_entry.get("dispositions", {}).items()
            if enabled
        ]
        disposition_value = "+".join(active_dispositions) if active_dispositions else "0"
        cmd.extend([f"-disposition:{stream_letter}:{output_index}", disposition_value])

    def _is_streaming_normalization_enabled(self):
        return bool(
            self.settings.get("audio_normalize_streaming", False)
            and self.settings.get("audio_mode", "convert") != "copy"
        )

    def _apply_audio_normalization_filters(self, cmd, mapped_container_tracks):
        if not self._is_streaming_normalization_enabled():
            return

        if self.target_format in VIDEO_CONTAINER_OUTPUTS and mapped_container_tracks is not None:
            audio_entries = mapped_container_tracks.get("audio", [])
            if not audio_entries:
                return

            for output_index, _track_entry in enumerate(audio_entries):
                cmd.extend([f"-filter:a:{output_index}", STREAMING_LOUDNORM_FILTER])

            logging.info(
                "Normalisation streaming appliquee sur %s piste(s) audio de sortie.",
                len(audio_entries),
            )
            return

        cmd.extend(["-filter:a", STREAMING_LOUDNORM_FILTER])
        logging.info("Normalisation streaming appliquee sur la sortie audio.")

    def _get_target_audio_codec(self):
        return resolve_audio_codec_key(self.target_format, self.settings)

    def _apply_encoded_audio_settings(self, cmd, mapped_container_tracks):
        apply_common_audio_options(cmd, self.settings)
        apply_audio_codec_args(cmd, self._get_target_audio_codec(), self.settings)
        self._apply_audio_normalization_filters(cmd, mapped_container_tracks)

    def _filter_subtitle_entries_for_container(self, subtitle_entries):
        if self.target_format not in ['mp4', 'mov']:
            return subtitle_entries

        compatible_entries = []
        for track_entry in subtitle_entries:
            codec_name = str(track_entry.get("codec_name", "")).lower()
            original_index = track_entry.get("original_index")

            if codec_name in MP4_TEXT_SUBTITLE_CODECS:
                if codec_name != "mov_text":
                    logging.info(
                        "Sous-titre #%s (%s) converti en mov_text pour %s.",
                        original_index,
                        codec_name or "unknown",
                        self.target_format.upper(),
                    )
                compatible_entries.append(track_entry)
                continue

            logging.warning(
                "Sous-titre #%s (%s) ignore pour %s car non compatible avec ce conteneur.",
                original_index,
                codec_name or "unknown",
                self.target_format.upper(),
            )

        return compatible_entries

    def _apply_video_container_track_mapping(self, cmd):
        effective_track_settings = get_effective_track_settings(self.meta)
        kept_video_tracks = get_kept_track_entries(effective_track_settings, "video")
        if not kept_video_tracks:
            logging.error("Aucune piste vidéo conservée pour la sortie vidéo (%s)", self.input_path)
            raise Exception(f"No video track selected for {os.path.basename(self.input_path)}")

        mapping_used = "personnalise" if getattr(self.meta, "track_settings", None) else "par defaut"
        logging.info("Utilisation du mapping vidéo explicite (%s).", mapping_used)

        mapped_entries = {
            "video": get_kept_track_entries(effective_track_settings, "video"),
            "audio": get_kept_track_entries(effective_track_settings, "audio"),
            "subtitle": self._filter_subtitle_entries_for_container(
                get_kept_track_entries(effective_track_settings, "subtitle")
            ),
        }

        for track_type in ("video", "audio", "subtitle"):
            kept_entries = mapped_entries[track_type]
            for output_index, track_entry in enumerate(kept_entries):
                cmd.extend(["-map", f"0:{track_entry['original_index']}"])
                self._apply_track_entry_metadata(cmd, track_type, output_index, track_entry)

        return mapped_entries

    def _build_image_command(self, output_path):
        cmd = [self.ffmpeg_exe, '-y', '-i', self.input_path]

        vf_filters = []
        resize = self.settings.get('image_resize', 'original')
        if resize and resize != 'original' and 'x' in resize:
            parts = resize.split('x', 1)
            try:
                w, h = int(parts[0]), int(parts[1])
                if w > 0 and h > 0:
                    vf_filters.append(f"scale={w}:{h}:force_original_aspect_ratio=decrease")
            except (ValueError, IndexError):
                logging.warning("Valeur de resize invalide ignorée: %s", resize)

        if vf_filters:
            cmd.extend(['-vf', ','.join(vf_filters)])

        fmt = self.target_format
        if fmt == 'jpeg':
            quality = max(1, min(100, int(self.settings.get('image_quality', 85))))
            qv = max(2, min(31, 31 - int((quality - 1) * 29 / 99)))
            cmd.extend(['-q:v', str(qv)])
        elif fmt == 'png':
            compression = max(0, min(9, int(self.settings.get('image_compression', 6))))
            cmd.extend(['-compression_level', str(compression)])
        elif fmt == 'webp':
            if self.settings.get('image_lossless', False):
                cmd.extend(['-c:v', 'libwebp', '-lossless', '1'])
            else:
                quality = max(0, min(100, int(self.settings.get('image_quality', 80))))
                cmd.extend(['-c:v', 'libwebp', '-quality', str(quality)])
        elif fmt == 'tiff':
            # Le jeton « non compressé » de l'encodeur TIFF FFmpeg est 'raw', pas
            # 'none' (qui est rejeté au parsing de -compression_algo).
            valid_tiff = ('lzw', 'deflate', 'packbits', 'raw')
            compression = str(self.settings.get('image_compression', 'lzw')).lower()
            if compression == 'none':
                compression = 'raw'
            if compression not in valid_tiff:
                compression = 'lzw'
            cmd.extend(['-compression_algo', compression])

        cmd.append('-an')

        thread_count = parse_ffmpeg_threads(self.settings)
        if thread_count is not None:
            cmd.extend(['-threads', str(thread_count)])

        cmd.append(output_path)
        return cmd

    def _run_image_conversion(self, output_path):
        cmd = self._build_image_command(output_path)
        self.last_command = list(cmd)
        logging.info(f"Commande FFmpeg (image): {' '.join(cmd)}")

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            universal_newlines=True, encoding='utf-8', errors='ignore',
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW
        )

        try:
            _, stderr_output = self.process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()
            raise Exception("FFmpeg image conversion timed out after 120 seconds") from None

        if stderr_output:
            for line in stderr_output.strip().splitlines()[-50:]:
                self.stderr_lines.append(line.strip())

        if self.process.returncode != 0:
            logging.error(f"FFmpeg image a échoué avec le code {self.process.returncode}")
            tail = "\n".join(self.stderr_lines[-50:])
            raise Exception(f"FFmpeg error (code {self.process.returncode}):\n{tail}")

        logging.info("Conversion image terminée avec succès.")

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
                logging.info("Processus FFmpeg interrompu pour: %s", self.input_path)
            except Exception:
                logging.exception("Impossible d'interrompre FFmpeg pour: %s", self.input_path)

    def _probe_duration(self, path):
        """Renvoie la durée (secondes) d'un fichier via ffprobe, ou None si illisible."""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            result = subprocess.run(
                [
                    self.ffprobe_exe, '-v', 'error',
                    '-show_entries', 'format=duration',
                    '-of', 'default=noprint_wrappers=1:nokey=1', path,
                ],
                capture_output=True, text=True, timeout=30,
                startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return float(result.stdout.strip())
        except (ValueError, subprocess.SubprocessError, OSError):
            return None

    def _output_looks_truncated(self, output_path):
        """Heuristique conservatrice : une conversion saine conserve la durée.

        On ne contrôle que les médias temporels dont la durée source est connue et
        non négligeable, et on ne signale que les sorties manifestement amputées
        (moins de la moitié de la source) afin d'éviter tout faux positif.
        """
        if not self.duration or self.duration <= 5:
            return False
        if not os.path.isfile(output_path):
            return True
        out_duration = self._probe_duration(output_path)
        if out_duration is None:
            return True
        return out_duration < self.duration * 0.5

    def run(self, progress_callback=None, stop_check_callback=None, drop_cover=False):
        if not os.path.isfile(self.input_path):
            logging.error("Fichier d'entrée introuvable au moment de la conversion : %s", self.input_path)
            raise FileNotFoundError(
                _translatef(
                    "File not found (it may have been moved or deleted): {name}",
                    name=os.path.basename(self.input_path),
                )
            )

        output_path = self.output_path
        if not output_path:
            output_path = build_output_path(
                self.input_path,
                self.target_format,
                custom_output_dir=self.custom_output_dir,
            )

        output_dir = os.path.dirname(output_path) or os.getcwd()
        # exist_ok : plusieurs pistes d'un même cue créent le sous-dossier album en
        # parallèle (sinon WinError 183 sur le perdant de la course).
        os.makedirs(output_dir, exist_ok=True)

        if self.target_format in IMAGE_OUTPUT_FORMAT_KEYS:
            return self._run_image_conversion(output_path)

        overrides = get_metadata_overrides(self.meta) if self.meta is not None else {}
        override_tags = overrides.get('tags', {}) if overrides else {}
        cover = overrides.get('cover', {}) if overrides else {}
        cover_action = cover.get('action', 'keep')

        audio_output = self.target_format not in VIDEO_CONTAINER_OUTPUTS
        cover_capable = self.target_format in COVER_ART_AUDIO_OUTPUTS
        cover_replace = cover_action == 'replace' and audio_output and cover_capable
        cover_path = cover.get('path') if cover_replace else None

        cmd = [self.ffmpeg_exe, '-y']
        if is_transport_stream(self.input_path):
            # PTS manquants / DTS non-monotones fréquents sur les .ts broadcast.
            cmd.extend(['-fflags', '+genpts'])
        clip_start_ms = clip_end_ms = None
        if self.clip:
            clip_start_ms, clip_end_ms = self.clip
            # -ss avant -i : seek d'entrée rapide et précis en réencodage.
            cmd.extend(['-ss', _format_ffmpeg_time(clip_start_ms)])
        cmd.extend(['-i', self.input_path])
        if cover_replace and cover_path:
            cmd.extend(['-i', cover_path])  # 2e entrée = nouvelle pochette (index 1)
        if clip_start_ms is not None and clip_end_ms is not None and clip_end_ms > clip_start_ms:
            # -t après toutes les entrées → s'applique à la sortie (durée de la piste).
            cmd.extend(['-t', _format_ffmpeg_time(clip_end_ms - clip_start_ms)])
        mapped_container_tracks = None

        if self.target_format in VIDEO_CONTAINER_OUTPUTS and self.meta is not None:
            mapped_container_tracks = self._apply_video_container_track_mapping(cmd)
        else:
            logging.debug("Mode automatique (pas de mapping vidéo explicite)")

        if self._is_video_to_audio_conversion():
            selected_track, selection_source = self._resolve_audio_extract_track()
            if selected_track is not None:
                cmd.extend(['-map', f"0:{selected_track.index}"])
                self._apply_audio_track_metadata(cmd, selected_track)
                logging.info(
                    "Piste audio d'extraction utilisée (%s) : stream #%s",
                    selection_source,
                    selected_track.index,
                )
            else:
                logging.warning("Aucune piste audio explicite n'a pu être sélectionnée pour l'extraction.")

        if cover_replace and cover_path:
            # Une fois la pochette mappée, la sélection auto est désactivée :
            # mapper explicitement l'audio (sauf si déjà fait pour l'extraction).
            if not self._is_video_to_audio_conversion():
                cmd.extend(['-map', '0:a'])
            cmd.extend(['-map', '1:0'])

        if self.clip:
            # Découpage cue : tags explicites par piste, sans recopie des métadonnées
            # de l'image (sinon le titre de l'album contaminerait chaque piste).
            preserve_metadata = False
            if self.extra_tags:
                cmd.extend(build_tag_metadata_args(self.extra_tags))
        else:
            preserve_metadata = apply_metadata_preservation(cmd, self.settings)

            if overrides_are_effective(overrides):
                # L'édition conserve les tags non modifiés, puis surcharge les champs édités.
                if not preserve_metadata:
                    cmd.extend(['-map_metadata', '0', '-map_chapters', '0'])
                    preserve_metadata = True
                cmd.extend(build_tag_metadata_args(override_tags))

            if self.target_format == 'm4b' and not preserve_metadata:
                # Le M4B est un livre audio : on conserve toujours les chapitres (et
                # tags) de la source lors d'une conversion d'un seul fichier.
                cmd.extend(['-map_metadata', '0', '-map_chapters', '0'])
                preserve_metadata = True

        audio_mode = self.settings.get('audio_mode', 'convert')
        if audio_mode == 'copy':
            cmd.extend(['-c:a', 'copy'])
        else:
            self._apply_encoded_audio_settings(cmd, mapped_container_tracks)

        used_cover_copy = False
        if self.target_format in VIDEO_CONTAINER_OUTPUTS:
            apply_video_codec_args(cmd, self.settings)

            if mapped_container_tracks and mapped_container_tracks.get("subtitle"):
                if self.target_format in ['mp4', 'mov']:
                    cmd.extend(['-c:s', 'mov_text'])
                elif self.target_format == 'mkv':
                    cmd.extend(['-c:s', 'copy'])
        else:
            if cover_replace and cover_path:
                # Nouvelle pochette (éditeur de métadonnées) : copier le flux image
                # ajouté en 2e entrée et le marquer attached_pic.
                cmd.extend(['-c:v', 'copy'])
                cmd.extend(cover_stream_args(0))
            elif (
                preserve_metadata
                and self.target_format in COVER_ART_AUDIO_OUTPUTS
                and not self._is_video_to_audio_conversion()
                and not drop_cover
            ):
                # Source sans vraie piste vidéo : tenter de conserver la pochette
                # attached_pic d'origine (sélection de flux par défaut). ATTENTION :
                # certaines pochettes (podcasts Radio France) sont un flux mjpeg à
                # paquet unique SANS timestamp (PTS=N/A) et de durée = celle du
                # fichier ; FFmpeg ne sait pas les ordonner dans le mux et tronque
                # tout l'audio (sortie ~20 Ko en code 0). On détecte ce cas après coup
                # (durée de sortie << durée source) et on relance sans pochette : voir
                # la validation en fin de run() (drop_cover=True).
                cmd.extend(['-c:v', 'copy'])
                used_cover_copy = True
            else:
                # Sortie audio sans pochette à conserver : on supprime le flux vidéo.
                # Les tags et chapitres restent préservés via -map_metadata /
                # -map_chapters appliqués plus haut.
                cmd.append('-vn')

        thread_count = parse_ffmpeg_threads(self.settings)
        if thread_count is not None:
            cmd.extend(['-threads', str(thread_count)])

        if self.target_format == 'm4b':
            # Le muxer ipod gère .m4b (sinon FFmpeg ne déduit pas le conteneur).
            cmd.extend(['-f', 'ipod'])

        cmd.append(output_path)
        self.last_command = list(cmd)

        logging.info("Commande FFmpeg: %s", ' '.join(cmd))

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            universal_newlines=True, encoding='utf-8', errors='ignore',
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW
        )

        time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2}\.\d+)')

        while True:
            if stop_check_callback and stop_check_callback():
                logging.info("Interruption demandée par l'utilisateur.")
                self.process.kill()
                raise Exception("Stopped by user")

            line = self.process.stderr.readline()
            if not line and self.process.poll() is not None:
                break

            if line:
                stripped = line.strip()
                logging.debug(f"FFmpeg output: {stripped}")
                self.stderr_lines.append(stripped)
                if len(self.stderr_lines) > 200:
                    self.stderr_lines.pop(0)

                if progress_callback:
                    match = time_pattern.search(line)
                    if match and self.duration > 0:
                        try:
                            h, m, s = match.groups()
                            current_seconds = int(h) * 3600 + int(m) * 60 + float(s)
                            percent = int((current_seconds / self.duration) * 100)
                            progress_callback(min(max(percent, 0), 100))
                        except (ValueError, TypeError):
                            pass

        if self.process.returncode != 0:
            if stop_check_callback and stop_check_callback():
                raise Exception("Stopped by user")
            logging.error(f"FFmpeg a échoué avec le code {self.process.returncode}")
            tail = "\n".join(self.stderr_lines[-50:])
            raise Exception(f"FFmpeg error (code {self.process.returncode}):\n{tail}")
        else:
            # Filet anti-échec-silencieux : FFmpeg renvoie parfois le code 0 tout en
            # produisant une sortie tronquée (pochette attached_pic ingérable, source
            # corrompue, etc.). On compare la durée produite à la durée source.
            if self._output_looks_truncated(output_path):
                if used_cover_copy and not drop_cover:
                    logging.warning(
                        "Sortie tronquée avec copie de pochette (%s) : nouvelle tentative sans pochette (-vn).",
                        os.path.basename(output_path),
                    )
                    return self.run(progress_callback, stop_check_callback, drop_cover=True)
                tail = "\n".join(self.stderr_lines[-50:])
                logging.error("Fichier de sortie anormalement court : %s", output_path)
                raise Exception(
                    _translate(
                        "The converted file is unexpectedly short — the conversion likely failed."
                    )
                    + (f"\n{tail}" if tail else "")
                )
            logging.info("Conversion terminée avec succès.")
