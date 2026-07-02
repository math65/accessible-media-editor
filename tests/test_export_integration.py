"""End-to-end export tests that drive the *real* embedded FFmpeg.

Unlike the pure unit tests, these actually cut audio and re-read the output
duration, so they exercise the highest-risk seams carried over from AMC:

- SegmentExportTask re-encode (filter_complex trim/atrim/concat)
- SegmentExportTask copy (per-region -c copy + concat demuxer)
- ConversionTask(clip=...) — the per-piece cut used by the N-files split mode

They are marked ``integration`` and auto-skip if FFmpeg is unavailable.
Run only the fast pure suite with:  pytest -m "not integration"
Run only these with:                pytest -m integration
"""

import os
import subprocess
from types import SimpleNamespace

import pytest

from core import segments as sg
from core.conversion import ConversionTask
from core.ffmpeg_helpers import get_ffmpeg_path, get_ffprobe_path
from core.formatting import normalize_format_settings
from core.segment_export import SegmentExportTask

FFMPEG = get_ffmpeg_path()
FFPROBE = get_ffprobe_path()

SOURCE_MS = 15_000
# Keep [0,5s] and [8s,15s]; discard the middle [5s,8s]. Kept total = 12s.
KEEP = [(0, 5_000), (8_000, 15_000)]
KEPT_MS = 12_000


def _ffmpeg_available():
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


# Every test in this module is an integration test and needs a working FFmpeg.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ffmpeg_available(), reason="embedded FFmpeg not runnable"),
]


def _probe_duration_s(path):
    """Output file duration in seconds via the embedded ffprobe."""
    out = subprocess.run(
        [FFPROBE, "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=30,
    )
    return float(out.stdout.strip())


@pytest.fixture(scope="module")
def source_mp3(tmp_path_factory):
    """A 15s sine-tone MP3, generated once with the embedded FFmpeg."""
    path = str(tmp_path_factory.mktemp("media") / "source.mp3")
    subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={SOURCE_MS / 1000:.0f}",
         "-c:a", "libmp3lame", "-q:a", "5", path],
        capture_output=True, timeout=60, check=True,
    )
    assert os.path.isfile(path)
    return path


def _plan():
    plan = sg.new_plan(SOURCE_MS)
    # Discard the middle region; the rest stays kept.
    sg.mark_region(plan, 5_000, 8_000, keep=False)
    assert sg.kept_regions(plan) == KEEP
    return plan


def test_reencode_single_file_matches_kept_duration(source_mp3, tmp_path):
    """Strategy B: filter_complex trim/atrim/concat -> one re-joined WAV."""
    out = str(tmp_path / "cut_reencode.wav")
    meta = SimpleNamespace(full_path=source_mp3)
    settings = {**normalize_format_settings("wav", {}), "audio_mode": "convert"}

    SegmentExportTask(meta, sg.kept_regions(_plan()), "wav", settings, out).run()

    assert os.path.isfile(out)
    assert _probe_duration_s(out) == pytest.approx(KEPT_MS / 1000, abs=0.4)


def test_copy_concat_single_file_matches_kept_duration(source_mp3, tmp_path):
    """Strategy A: per-region -c copy + concat demuxer -> one re-joined MP3."""
    out = str(tmp_path / "cut_copy.mp3")
    meta = SimpleNamespace(full_path=source_mp3)
    settings = {**normalize_format_settings("mp3", {}), "audio_mode": "copy"}

    SegmentExportTask(meta, sg.kept_regions(_plan()), "mp3", settings, out).run()

    assert os.path.isfile(out)
    # Looser tolerance: stream-copy cuts snap to frame boundaries.
    assert _probe_duration_s(out) == pytest.approx(KEPT_MS / 1000, abs=0.75)


def test_clip_piece_matches_region_duration(source_mp3, tmp_path):
    """N-files mode cuts each kept region via ConversionTask(clip=...)."""
    out = str(tmp_path / "piece.mp3")
    settings = normalize_format_settings("mp3", {})

    # Second kept region [8s, 15s] -> a 7s piece.
    ConversionTask(source_mp3, "mp3", settings, output_path=out, clip=(8_000, 15_000)).run()

    assert os.path.isfile(out)
    assert _probe_duration_s(out) == pytest.approx(7.0, abs=0.4)
