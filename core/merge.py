import builtins
import logging
import os
import re
import subprocess
import tempfile

from core.ffmpeg_helpers import (
    STREAMING_LOUDNORM_FILTER,
    VIDEO_CONTAINER_OUTPUTS,
    apply_audio_codec_args,
    apply_common_audio_options,
    apply_metadata_preservation,
    apply_video_codec_args,
    get_ffmpeg_path,
    is_transport_stream,
    parse_ffmpeg_threads,
    resolve_audio_codec_key,
)


def _translate(msgid):
    translator = builtins.__dict__.get('_')
    if callable(translator):
        return translator(msgid)
    return msgid


def _translatef(msgid, **kwargs):
    return _translate(msgid).format(**kwargs)


class MergeTask:
    def __init__(self, input_list, target_format, settings, output_path):
        self.input_list = input_list  # list of MediaMetadata
        self.target_format = target_format
        self.settings = settings
        self.output_path = output_path
        self.ffmpeg_exe = get_ffmpeg_path()
        self.process = None
        self.stderr_lines = []
        self.total_duration = sum(
            float(getattr(m, 'duration', 0) or 0) for m in input_list
        )

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
                logging.info("Processus FFmpeg de fusion interrompu.")
            except Exception:
                logging.exception("Impossible d'interrompre FFmpeg (fusion).")

    def _apply_audio_codec_settings(self, cmd):
        apply_common_audio_options(cmd, self.settings)
        apply_audio_codec_args(cmd, resolve_audio_codec_key(self.target_format, self.settings), self.settings)

        if (
            self.settings.get("audio_normalize_streaming", False)
            and self.settings.get("audio_mode", "convert") != "copy"
        ):
            cmd.extend(['-filter:a', STREAMING_LOUDNORM_FILTER])

    @staticmethod
    def _escape_ffmetadata(value):
        # Dans un fichier FFMETADATA, =, ;, #, \ et le saut de ligne doivent être
        # échappés par un antislash.
        text = str(value)
        for char in ('\\', '=', ';', '#'):
            text = text.replace(char, '\\' + char)
        return text.replace('\n', '\\\n')

    def _chapter_title_for(self, index, meta):
        mode = self.settings.get('m4b_chapter_naming', 'title_or_number')
        tag_title = ''
        format_tags = getattr(meta, 'format_tags', None)
        if isinstance(format_tags, dict):
            tag_title = str(format_tags.get('title') or '').strip()

        if mode != 'numbered' and tag_title:
            return tag_title
        if mode == 'title_or_filename':
            stem = os.path.splitext(os.path.basename(meta.full_path))[0].strip()
            if stem:
                return stem
        # 'numbered', ou repli quand titre/nom manquent.
        return _translatef("Chapter {number}", number=index + 1)

    def _build_chapter_ffmetadata(self):
        """Construit le texte FFMETADATA (un [CHAPTER] par fichier d'entrée)."""
        lines = [';FFMETADATA1']
        start_ms = 0
        for index, meta in enumerate(self.input_list):
            duration = float(getattr(meta, 'duration', 0) or 0)
            end_ms = start_ms + int(round(duration * 1000))
            if end_ms <= start_ms:
                # Durée inconnue/nulle : chapitre de longueur nulle, on prévient.
                logging.warning(
                    "Durée nulle pour %s : chapitre M4B de longueur nulle.",
                    os.path.basename(meta.full_path),
                )
                end_ms = start_ms
            title = self._escape_ffmetadata(self._chapter_title_for(index, meta))
            lines.append('[CHAPTER]')
            lines.append('TIMEBASE=1/1000')
            lines.append(f'START={start_ms}')
            lines.append(f'END={end_ms}')
            lines.append(f'title={title}')
            start_ms = end_ms
        return '\n'.join(lines) + '\n'

    def run(self, progress_callback=None, stop_check_callback=None):
        for meta in self.input_list:
            if not os.path.isfile(meta.full_path):
                logging.error("Fichier d'entrée introuvable au moment de la fusion : %s", meta.full_path)
                raise FileNotFoundError(
                    _translatef(
                        "File not found (it may have been moved or deleted): {name}",
                        name=os.path.basename(meta.full_path),
                    )
                )

        is_m4b = self.target_format == 'm4b'
        meta_path = None

        list_fd, list_path = tempfile.mkstemp(suffix='.txt', prefix='amc_concat_')
        try:
            with os.fdopen(list_fd, 'w', encoding='utf-8') as f:
                for meta in self.input_list:
                    # FFmpeg n'interprète aucun backslash dans une simple quote :
                    # une apostrophe se ferme via l'idiome '\'' (fermer, ' échappée,
                    # rouvrir). Ex. O'Brien.mp3 -> file 'O'\''Brien.mp3'.
                    path = meta.full_path.replace('\\', '/').replace("'", "'\\''")
                    f.write(f"file '{path}'\n")

            cmd = [self.ffmpeg_exe, '-y']
            if any(is_transport_stream(meta.full_path) for meta in self.input_list):
                # PTS manquants / DTS non-monotones fréquents sur les .ts broadcast.
                cmd.extend(['-fflags', '+genpts'])
            cmd.extend(['-f', 'concat', '-safe', '0', '-i', list_path])

            if is_m4b:
                # Chapitres : un [CHAPTER] par fichier fusionné, passé en 2e entrée.
                meta_fd, meta_path = tempfile.mkstemp(suffix='.ffmeta', prefix='amc_chapters_')
                with os.fdopen(meta_fd, 'w', encoding='utf-8') as mf:
                    mf.write(self._build_chapter_ffmetadata())
                cmd.extend(['-i', meta_path])

            if self.target_format in VIDEO_CONTAINER_OUTPUTS:
                apply_video_codec_args(cmd, self.settings)
                audio_mode = self.settings.get('audio_mode', 'convert')
                if audio_mode == 'copy':
                    cmd.extend(['-c:a', 'copy'])
                else:
                    self._apply_audio_codec_settings(cmd)
            else:
                audio_mode = self.settings.get('audio_mode', 'convert')
                if audio_mode == 'copy':
                    cmd.extend(['-c:a', 'copy'])
                else:
                    self._apply_audio_codec_settings(cmd)
                cmd.append('-vn')

            if is_m4b:
                # Audio du concat (entrée 0) + chapitres générés (entrée 1).
                cmd.extend(['-map', '0:a', '-map_chapters', '1'])
            else:
                # Conserve tags et chapitres du premier fichier fusionné si demandé
                # (la pochette n'est pas gérée pour les fusions concat).
                apply_metadata_preservation(cmd, self.settings)

            thread_count = parse_ffmpeg_threads(self.settings)
            if thread_count is not None:
                cmd.extend(['-threads', str(thread_count)])

            if is_m4b:
                cmd.extend(['-f', 'ipod'])  # muxer qui gère .m4b

            cmd.append(self.output_path)

            logging.info("Commande FFmpeg (fusion): %s", ' '.join(cmd))

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.PIPE,
                universal_newlines=True,
                encoding='utf-8',
                errors='ignore',
                startupinfo=startupinfo,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2}\.\d+)')

            while True:
                if stop_check_callback and stop_check_callback():
                    logging.info("Fusion : interruption demandée par l'utilisateur.")
                    self.process.kill()
                    raise Exception("Stopped by user")

                line = self.process.stderr.readline()
                if not line and self.process.poll() is not None:
                    break

                if line:
                    stripped = line.strip()
                    logging.debug("FFmpeg (merge): %s", stripped)
                    self.stderr_lines.append(stripped)
                    if len(self.stderr_lines) > 200:
                        self.stderr_lines.pop(0)

                    if progress_callback and self.total_duration > 0:
                        match = time_pattern.search(line)
                        if match:
                            try:
                                h, m, s = match.groups()
                                current_seconds = int(h) * 3600 + int(m) * 60 + float(s)
                                percent = int((current_seconds / self.total_duration) * 100)
                                progress_callback(min(max(percent, 0), 100))
                            except (ValueError, TypeError):
                                pass

            if self.process.returncode != 0:
                if stop_check_callback and stop_check_callback():
                    raise Exception("Stopped by user")
                logging.error("FFmpeg (fusion) a échoué avec le code %s", self.process.returncode)
                tail = "\n".join(self.stderr_lines[-50:])
                raise Exception(f"FFmpeg merge error (code {self.process.returncode}):\n{tail}")

            logging.info("Fusion terminée avec succès.")

        finally:
            try:
                os.unlink(list_path)
            except Exception:
                pass
            if meta_path:
                try:
                    os.unlink(meta_path)
                except Exception:
                    pass
