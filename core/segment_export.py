"""Export « 1 fichier reconcaténé » d'un plan de découpage manuel.

À partir des régions gardées d'un :class:`core.segments.SegmentPlan`, produit un
seul fichier de sortie où les régions jetées (ex. pubs) ont disparu. C'est une
opération **mono-sortie** (contrairement au split « N fichiers » qui passe par
``BatchConversionManager``), d'où une tâche calquée sur :class:`core.merge.MergeTask`
(thread worker + ``wx.CallAfter`` côté UI, ``stop()``, nettoyage temp).

Deux stratégies, choisies selon le réglage copie/réencodage déjà exposé :

- **Copie** (``*_mode == 'copy'``) → *Stratégie A* : chaque région est extraite en
  ``-c copy`` (coupe alignée sur keyframe, quasi instantanée) puis les morceaux
  sont réassemblés par le **concat demuxer**. Rapide, sans réencodage.
- **Réencodage** → *Stratégie B* : passe unique ``filter_complex`` (``trim``/``atrim``
  + ``setpts``/``asetpts`` + ``concat``). Précis à l'image et **un seul encode** sur
  toute la timeline jointe (évite les gaps/clics de priming entre segments
  réencodés séparément).

Simplifications de la phase 1 (documentées) : la stratégie B réencode **à la fois**
la vidéo et l'audio (un filtre ne peut pas copier) et ne conserve que la 1re piste
vidéo et la 1re piste audio ; la normalisation streaming et les chapitres ne sont
pas appliqués lors d'une découpe.
"""

import builtins
import logging
import os
import re
import subprocess
import tempfile

from core.ffmpeg_helpers import (
    VIDEO_CONTAINER_OUTPUTS,
    apply_audio_codec_args,
    apply_common_audio_options,
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


def _format_ffmpeg_time(ms):
    """Millisecondes → 'HH:MM:SS.mmm' accepté par ffmpeg (-ss/-t)."""
    total_seconds = max(0, ms) / 1000.0
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


class SegmentExportTask:
    def __init__(self, meta, regions, target_format, settings, output_path):
        self.meta = meta
        self.input_path = meta.full_path
        # regions : liste triée de tuples (start_ms, end_ms) — les régions gardées.
        self.regions = [tuple(r) for r in regions]
        self.target_format = target_format
        self.settings = dict(settings)
        self.output_path = output_path
        self.ffmpeg_exe = get_ffmpeg_path()
        self.process = None
        self.stderr_lines = []
        self.last_command = []
        self.total_kept_ms = sum(max(0, end - start) for start, end in self.regions)

    def stop(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.kill()
                logging.info("Processus FFmpeg de découpage interrompu.")
            except Exception:
                logging.exception("Impossible d'interrompre FFmpeg (découpage).")

    def _is_video_output(self):
        return self.target_format in VIDEO_CONTAINER_OUTPUTS and getattr(self.meta, 'has_video', False)

    def _is_copy_mode(self):
        audio_copy = self.settings.get('audio_mode', 'convert') == 'copy'
        if self._is_video_output():
            return audio_copy and self.settings.get('video_mode', 'convert') == 'copy'
        return audio_copy

    def run(self, progress_callback=None, stop_check_callback=None):
        if not os.path.isfile(self.input_path):
            logging.error("Fichier introuvable au moment du découpage : %s", self.input_path)
            raise FileNotFoundError(
                _translatef(
                    "File not found (it may have been moved or deleted): {name}",
                    name=os.path.basename(self.input_path),
                )
            )
        if not self.regions:
            raise Exception(_translate("At least one segment must be kept."))

        output_dir = os.path.dirname(self.output_path) or os.getcwd()
        os.makedirs(output_dir, exist_ok=True)

        if self._is_copy_mode():
            self._run_copy_concat(progress_callback, stop_check_callback)
        else:
            self._run_reencode_filter(progress_callback, stop_check_callback)

    # ------------------------------------------------------------------ Stratégie B
    def _run_reencode_filter(self, progress_callback, stop_check_callback):
        video = self._is_video_output()
        filters = []
        concat_inputs = []
        for i, (start_ms, end_ms) in enumerate(self.regions):
            # Secondes décimales, PAS le format HH:MM:SS : les « : » seraient pris
            # pour des séparateurs d'options par le parseur de filtres.
            start = f"{start_ms / 1000.0:.3f}"
            end = f"{end_ms / 1000.0:.3f}"
            if video:
                filters.append(f"[0:v:0]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]")
                concat_inputs.append(f"[v{i}]")
            filters.append(f"[0:a:0]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]")
            concat_inputs.append(f"[a{i}]")

        n = len(self.regions)
        if video:
            filters.append(f"{''.join(concat_inputs)}concat=n={n}:v=1:a=1[outv][outa]")
        else:
            filters.append(f"{''.join(concat_inputs)}concat=n={n}:v=0:a=1[outa]")
        filter_complex = ';'.join(filters)

        cmd = [self.ffmpeg_exe, '-y']
        if is_transport_stream(self.input_path):
            cmd.extend(['-fflags', '+genpts'])
        cmd.extend(['-i', self.input_path, '-filter_complex', filter_complex])

        if video:
            cmd.extend(['-map', '[outv]', '-map', '[outa]'])
            apply_video_codec_args(cmd, self.settings)
        else:
            cmd.extend(['-map', '[outa]', '-vn'])

        apply_common_audio_options(cmd, self.settings)
        apply_audio_codec_args(cmd, resolve_audio_codec_key(self.target_format, self.settings), self.settings)

        thread_count = parse_ffmpeg_threads(self.settings)
        if thread_count is not None:
            cmd.extend(['-threads', str(thread_count)])
        if self.target_format == 'm4b':
            cmd.extend(['-f', 'ipod'])
        cmd.append(self.output_path)

        logging.info("Commande FFmpeg (découpage réencodé): %s", ' '.join(cmd))
        self.last_command = list(cmd)
        # La sortie fait exactement la durée gardée : progression = time= / total.
        self._run_process(cmd, self.total_kept_ms, 0, progress_callback, stop_check_callback)

    # ------------------------------------------------------------------ Stratégie A
    def _run_copy_concat(self, progress_callback, stop_check_callback):
        source_ext = os.path.splitext(self.input_path)[1] or '.mkv'
        temp_segments = []
        list_path = None
        try:
            accumulated_ms = 0
            for i, (start_ms, end_ms) in enumerate(self.regions):
                seg_fd, seg_path = tempfile.mkstemp(suffix=source_ext, prefix='amc_cut_')
                os.close(seg_fd)
                temp_segments.append(seg_path)

                cmd = [self.ffmpeg_exe, '-y']
                if is_transport_stream(self.input_path):
                    cmd.extend(['-fflags', '+genpts'])
                cmd.extend(['-ss', _format_ffmpeg_time(start_ms), '-i', self.input_path])
                cmd.extend(['-t', _format_ffmpeg_time(end_ms - start_ms)])
                cmd.extend(['-c', 'copy', '-avoid_negative_ts', 'make_zero', seg_path])

                logging.info("Commande FFmpeg (extraction segment %d): %s", i + 1, ' '.join(cmd))
                self.last_command = list(cmd)
                self._run_process(
                    cmd, self.total_kept_ms, accumulated_ms,
                    progress_callback, stop_check_callback, progress_ceiling=95,
                )
                accumulated_ms += (end_ms - start_ms)

            # Concat demuxer : réassemble les morceaux copiés dans le conteneur cible.
            list_fd, list_path = tempfile.mkstemp(suffix='.txt', prefix='amc_cutlist_')
            with os.fdopen(list_fd, 'w', encoding='utf-8') as handle:
                for seg_path in temp_segments:
                    # Idiome de quoting FFmpeg : une apostrophe se ferme via '\''.
                    quoted = seg_path.replace('\\', '/').replace("'", "'\\''")
                    handle.write(f"file '{quoted}'\n")

            cmd = [self.ffmpeg_exe, '-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy']
            thread_count = parse_ffmpeg_threads(self.settings)
            if thread_count is not None:
                cmd.extend(['-threads', str(thread_count)])
            if self.target_format == 'm4b':
                cmd.extend(['-f', 'ipod'])
            cmd.append(self.output_path)

            logging.info("Commande FFmpeg (concat découpage): %s", ' '.join(cmd))
            self.last_command = list(cmd)
            self._run_process(
                cmd, self.total_kept_ms, 0,
                progress_callback, stop_check_callback, progress_floor=95, progress_ceiling=100,
            )
        finally:
            for seg_path in temp_segments:
                try:
                    os.unlink(seg_path)
                except OSError:
                    pass
            if list_path:
                try:
                    os.unlink(list_path)
                except OSError:
                    pass

    # ------------------------------------------------------------------ commun
    def _run_process(self, cmd, total_ms, base_ms, progress_callback, stop_check_callback,
                     progress_floor=0, progress_ceiling=100):
        """Lance un ffmpeg et suit sa progression via ``time=``. ``base_ms`` décale
        la position pour cumuler entre extractions ; ``progress_floor/ceiling``
        bornent le pourcentage rapporté (concat vs extractions)."""
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        self.process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE,
            universal_newlines=True, encoding='utf-8', errors='ignore',
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
        )

        time_pattern = re.compile(r'time=(\d{2}):(\d{2}):(\d{2}\.\d+)')

        while True:
            if stop_check_callback and stop_check_callback():
                logging.info("Découpage : interruption demandée par l'utilisateur.")
                self.process.kill()
                raise Exception("Stopped by user")

            line = self.process.stderr.readline()
            if not line and self.process.poll() is not None:
                break

            if line:
                stripped = line.strip()
                logging.debug("FFmpeg (cut): %s", stripped)
                self.stderr_lines.append(stripped)
                if len(self.stderr_lines) > 200:
                    self.stderr_lines.pop(0)

                if progress_callback and total_ms > 0:
                    match = time_pattern.search(line)
                    if match:
                        try:
                            h, m, s = match.groups()
                            current_ms = base_ms + (int(h) * 3600 + int(m) * 60 + float(s)) * 1000
                            span = max(1, progress_ceiling - progress_floor)
                            percent = progress_floor + (current_ms / total_ms) * span
                            progress_callback(int(min(max(percent, progress_floor), progress_ceiling)))
                        except (ValueError, TypeError):
                            pass

        if self.process.returncode != 0:
            if stop_check_callback and stop_check_callback():
                raise Exception("Stopped by user")
            logging.error("FFmpeg (découpage) a échoué avec le code %s", self.process.returncode)
            tail = "\n".join(self.stderr_lines[-50:])
            raise Exception(f"FFmpeg cut error (code {self.process.returncode}):\n{tail}")

        logging.info("Étape de découpage terminée avec succès.")
