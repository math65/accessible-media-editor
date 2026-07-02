"""Détection des silences via le filtre FFmpeg ``silencedetect``.

Sert à l'éditeur de découpe : sauter d'un blanc à l'autre pour trouver vite les
bornes d'une pub (les coupures publicitaires sont souvent précédées d'un silence).

``detect_silences`` décode l'audio en entier (donc potentiellement long sur un
film 2 h) — l'appelant le lance sur un thread et met en cache le résultat.
"""

import logging
import re
import subprocess


_START_RE = re.compile(r'silence_start:\s*(-?\d+(?:\.\d+)?)')
_END_RE = re.compile(r'silence_end:\s*(-?\d+(?:\.\d+)?)')


def detect_silences(path, ffmpeg_exe, noise_db=-30, min_duration_s=0.35, stop_check=None):
    """Renvoie les silences détectés sous forme de liste de tuples ``(start_ms,
    end_ms)`` triés. ``noise_db`` = seuil (dBFS), ``min_duration_s`` = durée
    minimale d'un silence. ``stop_check`` optionnel (callable → bool) permet
    d'interrompre. Renvoie [] en cas d'erreur."""
    cmd = [
        ffmpeg_exe, '-hide_banner', '-nostats',
        '-i', path,
        '-af', f'silencedetect=noise={noise_db}dB:d={min_duration_s}',
        '-f', 'null', '-',
    ]
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

    silences = []
    pending_start = None
    try:
        process = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            universal_newlines=True, encoding='utf-8', errors='ignore',
            startupinfo=startupinfo, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in process.stderr:
            if stop_check is not None and stop_check():
                process.kill()
                return silences
            start_match = _START_RE.search(line)
            if start_match:
                pending_start = int(round(float(start_match.group(1)) * 1000))
                continue
            end_match = _END_RE.search(line)
            if end_match and pending_start is not None:
                end_ms = int(round(float(end_match.group(1)) * 1000))
                if end_ms > pending_start:
                    silences.append((max(0, pending_start), end_ms))
                pending_start = None
        process.wait()
    except (OSError, ValueError):
        logging.exception("Détection des silences : échec pour %s", path)
        return silences
    return silences


def silence_points(silences):
    """Points de repère de navigation = milieu de chaque silence (un endroit sûr
    à l'intérieur du blanc), triés."""
    return sorted((start + end) // 2 for start, end in silences)
