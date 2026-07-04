"""Smart cut (« copie exacte ») — coupe image-exacte sans tout réencoder.

Pour une région gardée ``[start, end]``, seuls les **GOP tronqués aux bords** sont
réencodés ; tout l'intérieur est **copié sans perte**. C'est la technique du « smart
rendering » de VideoReDo / du « smart cut » de LosslessCut.

Recette (prototypée et validée — cf. mémoire *smart-cut-ffmpeg-recipe*) :

1. Sonder les keyframes vidéo dans la région. ``kf1`` = 1re keyframe ≥ start,
   ``kf2`` = dernière keyframe ≤ end (en indices d'images).
2. Vidéo découpée en trois morceaux **MPEG-TS** (SPS/PPS en ligne → le concat demuxer
   accepte le mélange réencodé/copié) :
   - **tête** ``[start, kf1)`` réencodée (paramètres calés sur la source) ;
   - **milieu** ``[kf1, kf2)`` **copié** ``-c:v copy`` par ``-frames:v N`` (jamais ``-t``,
     qui déborde de ~2 images sur le GOP suivant en coupant sur le DTS) ;
   - **queue** ``[kf2, end)`` réencodée.
3. Concat des trois. L'audio est **copié en un seul flux continu** ``[start, end]`` puis
   muxé avec la vidéo (pas de jointures audio → pas de clic de priming), comme VideoReDo.

Si la région ne contient pas de keyframe exploitable (tient dans un seul GOP) la région
entière est réencodée : ``SmartCutNotApplicable`` est levé pour que l'appelant bascule sur
son chemin de réencodage habituel.
"""

import json
import logging
import os
import subprocess
import tempfile

from core.ffmpeg_helpers import (
    get_ffmpeg_path,
    get_ffprobe_path,
    is_transport_stream,
)

FFPROBE_TIMEOUT_SECONDS = 60

# codec source (ffprobe codec_name) → encodeur libx* pour réencoder les bords en
# gardant le même codec (indispensable pour que le concat demuxer accepte le mélange).
_ENCODER_FOR_CODEC = {
    'h264': 'libx264',
    'hevc': 'libx265',
    'mpeg2video': 'mpeg2video',
    'mpeg4': 'mpeg4',
}


class SmartCutNotApplicable(Exception):
    """La région ne se prête pas au smart cut (pas de keyframe interne, codec non géré,
    pas de flux vidéo…). L'appelant doit réencoder la région entièrement."""


def _startup_info():
    if os.name == 'nt':
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return info
    return None


def _parse_rate(value):
    """'num/den' ou nombre → float ; 0.0 si illisible."""
    if not value:
        return 0.0
    text = str(value)
    try:
        if '/' in text:
            num, den = text.split('/', 1)
            den = float(den)
            return float(num) / den if den else 0.0
        return float(text)
    except (ValueError, TypeError):
        return 0.0


def probe_video_params(input_path):
    """Paramètres du 1er flux vidéo utiles au réencodage des bords : fps, codec,
    pix_fmt, profil, et présence d'un flux audio. Lève SmartCutNotApplicable si pas
    de vidéo ou codec non géré."""
    cmd = [
        get_ffprobe_path(), '-v', 'error', '-print_format', 'json',
        '-show_streams', input_path,
    ]
    output = subprocess.check_output(
        cmd, startupinfo=_startup_info(), timeout=FFPROBE_TIMEOUT_SECONDS,
    )
    streams = json.loads(output).get('streams', [])
    video = next((s for s in streams if s.get('codec_type') == 'video'
                  and s.get('disposition', {}).get('attached_pic', 0) != 1), None)
    if video is None:
        raise SmartCutNotApplicable("no video stream")

    codec = video.get('codec_name', '')
    encoder = _ENCODER_FOR_CODEC.get(codec)
    if not encoder:
        raise SmartCutNotApplicable("unsupported video codec: %s" % codec)

    fps = _parse_rate(video.get('avg_frame_rate')) or _parse_rate(video.get('r_frame_rate'))
    if fps <= 0:
        raise SmartCutNotApplicable("unknown frame rate")

    return {
        'fps': fps,
        'codec': codec,
        'encoder': encoder,
        'pix_fmt': video.get('pix_fmt') or 'yuv420p',
        'profile': (video.get('profile') or '').lower() or None,
        'has_audio': any(s.get('codec_type') == 'audio' for s in streams),
    }


def probe_keyframe_times(input_path, start_s, end_s):
    """Timestamps (s) des keyframes vidéo dans [start_s, end_s], triés. Fenêtré via
    -read_intervals pour ne pas scanner tout le fichier."""
    cmd = [
        get_ffprobe_path(), '-v', 'error', '-select_streams', 'v:0',
        '-show_packets', '-show_entries', 'packet=pts_time,flags',
        '-of', 'csv=print_section=0',
        '-read_intervals', '%f%%%f' % (max(0.0, start_s), end_s),
        input_path,
    ]
    output = subprocess.check_output(
        cmd, startupinfo=_startup_info(), timeout=FFPROBE_TIMEOUT_SECONDS,
        universal_newlines=True, encoding='utf-8', errors='ignore',
    )
    times = []
    for line in output.splitlines():
        parts = line.split(',')
        if len(parts) < 2 or not parts[0]:
            continue
        # flags : 'K__' pour une keyframe (1er caractère 'K').
        if 'K' in parts[1]:
            try:
                times.append(float(parts[0]))
            except ValueError:
                pass
    return sorted(times)


class SmartCutter:
    """Découpe une région en un fichier image-exact via réencodage des seuls bords.

    Instance porteuse d'un ``stop()`` (tue le ffmpeg courant) pour s'intégrer aux
    tâches d'export existantes qui gèrent déjà thread + annulation."""

    def __init__(self, threads=None):
        self.ffmpeg = get_ffmpeg_path()
        self.threads = threads
        self._process = None
        self._stopped = False

    def stop(self):
        self._stopped = True
        proc = self._process
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                logging.exception("SmartCutter: impossible de tuer ffmpeg.")

    def _run(self, cmd):
        if self._stopped:
            raise Exception("Stopped by user")
        logging.info("SmartCut ffmpeg: %s", ' '.join(cmd))
        self._process = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            startupinfo=_startup_info(), creationflags=subprocess.CREATE_NO_WINDOW,
            universal_newlines=True, encoding='utf-8', errors='ignore',
        )
        _out, err = self._process.communicate()
        if self._stopped:
            raise Exception("Stopped by user")
        if self._process.returncode != 0:
            tail = "\n".join((err or "").strip().splitlines()[-15:])
            raise Exception("ffmpeg smart-cut error (code %s):\n%s"
                            % (self._process.returncode, tail))

    def _encode_args(self, params):
        args = ['-c:v', params['encoder'], '-pix_fmt', params['pix_fmt'], '-preset', 'fast']
        if params['encoder'] in ('libx264', 'libx265') and params.get('profile'):
            # profils valides côté encodeur (baseline/main/high pour x264).
            args += ['-profile:v', params['profile']]
        if self.threads is not None:
            args += ['-threads', str(self.threads)]
        return args

    def cut_region(self, input_path, start_ms, end_ms, output_path, params=None):
        """Écrit `output_path` = région [start_ms, end_ms) de `input_path`, image-exacte.

        Lève SmartCutNotApplicable si la région ne contient pas de keyframe interne
        exploitable (l'appelant réencodera alors la région entièrement)."""
        if params is None:
            params = probe_video_params(input_path)
        fps = params['fps']
        start_s = start_ms / 1000.0
        end_s = end_ms / 1000.0

        f_start = int(round(start_s * fps))
        f_end = int(round(end_s * fps))
        if f_end <= f_start:
            raise SmartCutNotApplicable("empty region")

        # keyframes → indices d'images (fenêtre élargie d'1 s pour capter les bords).
        kf_times = probe_keyframe_times(input_path, max(0.0, start_s - 1.0), end_s + 1.0)
        kf_frames = sorted({int(round(t * fps)) for t in kf_times})
        kf1 = next((k for k in kf_frames if k >= f_start), None)
        kf2 = next((k for k in reversed(kf_frames) if k <= f_end), None)
        # Il faut une vraie zone copiable [kf1, kf2) strictement à l'intérieur.
        if kf1 is None or kf2 is None or kf2 <= kf1 or kf1 >= f_end:
            raise SmartCutNotApplicable("no interior keyframe span")

        ext = os.path.splitext(input_path)[1] or '.mkv'
        temps = []
        try:
            enc = self._encode_args(params)
            genpts = ['-fflags', '+genpts'] if is_transport_stream(input_path) else []

            def mktemp(suffix):
                fd, path = tempfile.mkstemp(suffix=suffix, prefix='ame_sc_')
                os.close(fd)
                temps.append(path)
                return path

            pieces = []

            # tête réencodée [f_start, kf1)
            if kf1 > f_start:
                head = mktemp('.ts')
                dur = (kf1 - f_start) / fps
                self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
                          + genpts + ['-ss', '%.6f' % start_s, '-i', input_path,
                          '-t', '%.6f' % dur, '-an'] + enc + ['-f', 'mpegts', head])
                pieces.append(head)

            # milieu copié [kf1, kf2) par nombre d'images exact
            mid = mktemp('.ts')
            n_mid = kf2 - kf1
            self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
                      + genpts + ['-ss', '%.6f' % (kf1 / fps), '-i', input_path,
                      '-c:v', 'copy', '-an', '-frames:v', str(n_mid), '-f', 'mpegts', mid])
            pieces.append(mid)

            # queue réencodée [kf2, f_end)
            if f_end > kf2:
                tail = mktemp('.ts')
                dur = (f_end - kf2) / fps
                self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
                          + genpts + ['-ss', '%.6f' % (kf2 / fps), '-i', input_path,
                          '-t', '%.6f' % dur, '-an'] + enc + ['-f', 'mpegts', tail])
                pieces.append(tail)

            # concat vidéo
            video_ts = mktemp('.ts')
            list_path = mktemp('.txt')
            with open(list_path, 'w', encoding='utf-8') as handle:
                for piece in pieces:
                    quoted = piece.replace('\\', '/').replace("'", "'\\''")
                    handle.write("file '%s'\n" % quoted)
            self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                       '-f', 'concat', '-safe', '0', '-i', list_path,
                       '-c', 'copy', '-f', 'mpegts', video_ts])

            # audio copié en un seul flux + mux (ou remux vidéo seule)
            if params.get('has_audio'):
                audio = mktemp(ext)
                self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error']
                          + genpts + ['-ss', '%.6f' % start_s, '-i', input_path,
                          '-t', '%.6f' % (end_s - start_s), '-vn', '-c:a', 'copy', audio])
                self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                           '-i', video_ts, '-i', audio, '-c', 'copy',
                           '-map', '0:v:0', '-map', '1:a:0', output_path])
            else:
                self._run([self.ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                           '-i', video_ts, '-c', 'copy', output_path])
        finally:
            for path in temps:
                try:
                    os.unlink(path)
                except OSError:
                    pass
