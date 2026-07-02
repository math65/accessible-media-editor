"""Shared FFmpeg utilities used by ConversionTask, MergeTask, and FileProber."""

import logging
import os
import sys


STREAMING_LOUDNORM_FILTER = "loudnorm=I=-16:TP=-1:LRA=7"

VIDEO_CONTAINER_OUTPUTS = ('mp4', 'mkv', 'mov')

# Formats audio dont le conteneur sait embarquer une pochette (attached_pic).
COVER_ART_AUDIO_OUTPUTS = ('mp3', 'aac', 'm4b', 'alac', 'flac')

_WAV_DEPTH_TO_CODEC = {'16': 'pcm_s16le', '24': 'pcm_s24le', '32': 'pcm_f32le'}

# Conteneurs MPEG-TS (flux de diffusion / captures TV, caméscopes AVCHD). Ces
# flux ont souvent des PTS manquants ou des DTS non-monotones ; `-fflags +genpts`
# régénère les PTS manquants à l'entrée (correctif documenté FFmpeg), ce qui
# fiabilise surtout les chemins `-c copy` vers MP4.
TRANSPORT_STREAM_EXTENSIONS = {'.ts', '.m2ts', '.mts'}


def is_transport_stream(path):
    """True si le chemin pointe vers un conteneur MPEG-TS (par extension)."""
    return os.path.splitext(path or '')[1].lower() in TRANSPORT_STREAM_EXTENSIONS


def _bin_path(executable):
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    return os.path.join(base_path, 'bin', executable)


def get_ffmpeg_path():
    candidate = _bin_path('ffmpeg.exe')
    if os.path.exists(candidate):
        return candidate
    logging.warning("ffmpeg.exe non trouvé dans bin/, utilisation du PATH système")
    return "ffmpeg"


def get_ffprobe_path():
    candidate = _bin_path('ffprobe.exe')
    if os.path.exists(candidate):
        return candidate
    return "ffprobe"


def parse_ffmpeg_threads(settings):
    value = settings.get("ffmpeg_threads", "auto")
    if isinstance(value, str) and value.lower() == "auto":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def apply_metadata_preservation(cmd, settings):
    """Ajoute les drapeaux conservant tags globaux et chapitres si l'option est active.

    Renvoie True si la conservation est demandée, pour permettre à l'appelant
    de décider en plus du sort de la pochette (attached_pic).
    """
    if settings.get('preserve_metadata', False):
        cmd.extend(['-map_metadata', '0', '-map_chapters', '0'])
        return True
    return False


def apply_common_audio_options(cmd, settings):
    sample_rate = settings.get('audio_sample_rate', 'original')
    if sample_rate != 'original':
        cmd.extend(['-ar', sample_rate])

    channels = settings.get('audio_channels', 'original')
    if channels == '2':
        cmd.extend(['-ac', '2'])
    elif channels == '1':
        cmd.extend(['-ac', '1'])


def resolve_audio_codec_key(target_format, settings):
    """Clé de codec audio effective pour un format de sortie.

    Un conteneur vidéo délègue à ``get_effective_audio_codec`` (aac/…), le M4B est
    un MP4 encodé en AAC, sinon le format audio est sa propre clé. Partagé par
    ConversionTask, MergeTask et SegmentExportTask pour éviter la divergence.
    """
    # Import paresseux : formatting est un module de plus haut niveau ; on évite
    # tout risque de cycle à l'import de ce module bas niveau.
    from core.formatting import get_effective_audio_codec

    if target_format in VIDEO_CONTAINER_OUTPUTS:
        return get_effective_audio_codec(target_format, settings)
    if target_format == 'm4b':
        return 'aac'
    return target_format


def apply_video_codec_args(cmd, settings):
    """Émet les arguments d'encodage vidéo H.264 (ou ``-c:v copy``) selon les
    réglages. Partagé par ConversionTask, MergeTask et SegmentExportTask.
    """
    if settings.get('video_mode', 'convert') == 'copy':
        cmd.extend(['-c:v', 'copy'])
        return

    crf = str(settings.get('video_crf', 23))
    encoder_preset = str(settings.get('video_encoder_preset', 'medium') or 'medium')
    pixel_format = str(settings.get('video_pixel_format', 'yuv420p') or 'yuv420p')
    cmd.extend(['-c:v', 'libx264', '-crf', crf, '-preset', encoder_preset, '-pix_fmt', pixel_format])

    if pixel_format == 'yuv420p':
        video_profile = str(settings.get('video_profile', 'high') or 'high')
        cmd.extend(['-profile:v', video_profile])
    else:
        logging.info(
            "Profil H.264 ignoré pour le pixel format %s afin d'éviter une combinaison invalide.",
            pixel_format,
        )


def apply_audio_codec_args(cmd, codec_key, settings):
    if codec_key == 'mp3':
        cmd.extend(['-c:a', 'libmp3lame'])
        mode = settings.get('rate_mode', 'cbr')
        if mode == 'vbr':
            cmd.extend(['-q:a', str(settings.get('audio_qscale', 0))])
        elif mode == 'abr':
            # ABR : débit moyen ciblé (libmp3lame n'a pas de borne min/max VBR).
            cmd.extend(['-abr', '1', '-b:a', settings.get('audio_bitrate', '192k')])
        else:  # cbr
            cmd.extend(['-b:a', settings.get('audio_bitrate', '192k')])
    elif codec_key == 'aac':
        cmd.extend(['-c:a', 'aac'])
        if settings.get('rate_mode', 'cbr') == 'cbr':
            cmd.extend(['-b:a', settings.get('audio_bitrate', '192k')])
        else:
            cmd.extend(['-q:a', str(settings.get('audio_qscale', 3))])
    elif codec_key == 'opus':
        cmd.extend(['-c:a', 'libopus', '-b:a', settings.get('audio_bitrate', '192k')])
    elif codec_key == 'ogg':
        cmd.extend(['-c:a', 'libvorbis', '-q:a', str(settings.get('audio_qscale', 6))])
    elif codec_key == 'wma':
        cmd.extend(['-c:a', 'wmav2', '-b:a', settings.get('audio_bitrate', '128k')])
    elif codec_key == 'wav':
        depth = settings.get('audio_bit_depth', 'original')
        cmd.extend(['-c:a', _WAV_DEPTH_TO_CODEC.get(str(depth), 'pcm_s16le')])
    elif codec_key == 'flac':
        cmd.extend(['-c:a', 'flac', '-compression_level', str(settings.get('flac_compression', 5))])
        depth = settings.get('audio_bit_depth', 'original')
        if depth == '16':
            cmd.extend(['-sample_fmt', 's16'])
        elif depth == '24':
            cmd.extend(['-sample_fmt', 's32'])
    elif codec_key == 'alac':
        cmd.extend(['-c:a', 'alac'])
        depth = settings.get('audio_bit_depth', 'original')
        if depth == '16':
            cmd.extend(['-sample_fmt', 's16p'])
        elif depth == '24':
            cmd.extend(['-sample_fmt', 's32p'])
