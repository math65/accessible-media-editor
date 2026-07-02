"""Tests for the pure FFmpeg argument builders (core/ffmpeg_helpers.py).

These functions build command-line lists and never touch FFmpeg itself, so they
are fully unit-testable.
"""

from core import ffmpeg_helpers as fh


def test_is_transport_stream():
    assert fh.is_transport_stream("capture.ts") is True
    assert fh.is_transport_stream("clip.M2TS") is True  # case-insensitive
    assert fh.is_transport_stream("movie.mp4") is False
    assert fh.is_transport_stream("") is False


def test_parse_ffmpeg_threads():
    assert fh.parse_ffmpeg_threads({}) is None  # defaults to "auto"
    assert fh.parse_ffmpeg_threads({"ffmpeg_threads": "auto"}) is None
    assert fh.parse_ffmpeg_threads({"ffmpeg_threads": "4"}) == 4
    assert fh.parse_ffmpeg_threads({"ffmpeg_threads": "0"}) == 1  # clamped to >= 1
    assert fh.parse_ffmpeg_threads({"ffmpeg_threads": "nope"}) is None


def test_apply_metadata_preservation():
    cmd = []
    assert fh.apply_metadata_preservation(cmd, {"preserve_metadata": True}) is True
    assert cmd == ["-map_metadata", "0", "-map_chapters", "0"]

    cmd = []
    assert fh.apply_metadata_preservation(cmd, {}) is False
    assert cmd == []


def test_apply_common_audio_options():
    cmd = []
    fh.apply_common_audio_options(cmd, {"audio_sample_rate": "44100", "audio_channels": "1"})
    assert cmd == ["-ar", "44100", "-ac", "1"]

    cmd = []
    fh.apply_common_audio_options(cmd, {})  # all "original" -> nothing added
    assert cmd == []


def test_apply_video_codec_args_copy_vs_encode():
    cmd = []
    fh.apply_video_codec_args(cmd, {"video_mode": "copy"})
    assert cmd == ["-c:v", "copy"]

    cmd = []
    fh.apply_video_codec_args(cmd, {})  # defaults to convert
    assert cmd[:2] == ["-c:v", "libx264"]
    assert "-crf" in cmd and "-preset" in cmd


def test_apply_audio_codec_args_mp3_cbr():
    cmd = []
    fh.apply_audio_codec_args(cmd, "mp3", {"rate_mode": "cbr", "audio_bitrate": "192k"})
    assert cmd == ["-c:a", "libmp3lame", "-b:a", "192k"]


def test_apply_audio_codec_args_wav_depth_maps_to_pcm():
    cmd = []
    fh.apply_audio_codec_args(cmd, "wav", {"audio_bit_depth": "24"})
    assert cmd == ["-c:a", "pcm_s24le"]

    cmd = []
    fh.apply_audio_codec_args(cmd, "wav", {"audio_bit_depth": "original"})
    assert cmd == ["-c:a", "pcm_s16le"]  # unknown depth falls back to 16-bit


def test_resolve_audio_codec_key():
    # m4b is an AAC-in-MP4 container; a plain audio format is its own key.
    assert fh.resolve_audio_codec_key("m4b", {}) == "aac"
    assert fh.resolve_audio_codec_key("mp3", {}) == "mp3"
    assert fh.resolve_audio_codec_key("flac", {}) == "flac"
