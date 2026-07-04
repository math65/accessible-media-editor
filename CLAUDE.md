# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Accessible Media Editor** (AME) — a Windows desktop **audio/video editor** for blind and
visually impaired users, built with `wxPython` and embedded `FFmpeg`. Accessibility (NVDA,
100 % keyboard workflows) is the top design priority. Current version: `0.1.0` (early seed).

**Origin.** This project was **bootstrapped by extracting the file cutter / segment editor**
out of its sibling app *Accessible Media Converter* (AMC, `C:\Users\mathi\dev\UniversalTranscoder`).
The whole cutter — segment model, the debugged PortAudio playback engine, and the export
pipeline — was carried over **working, not rewritten**. What is new here is only the standalone
shell (entry point + export host) that replaces the AMC main window the editor used to live in.

**Vision / roadmap (to design in future sessions).** Today AME does what the cutter did: open one
temporal media file, mark keep/discard regions, listen (play / montage / verify-cut / scrub /
silence-jump), and export one re-joined file or N separate files. The goal is a **real editor**:
multi-file timeline, fades/crossfades, effects, maybe recording. None of that exists yet — it is
the open work.

## Running and building

Dependencies are managed with **uv** (`pyproject.toml` + `uv.lock`). Runtime deps: `wxPython`,
`accessible_output2`, `sounddevice`. Dev group: `pyinstaller`, `polib`.

```powershell
uv sync            # create .venv and install runtime + dev deps
uv run main.py     # run from source (opens a file picker, then the editor)
```

`main.py` also accepts a file path argument (for a future "Open with…" Explorer verb).

**Packaging (rewired, not yet validated end-to-end).** The seed carries AMC's build tooling renamed:
`AccessibleMediaEditor.spec`, `scripts/build_release.ps1`, `scripts/update_embedded_ffmpeg.ps1`,
`installer/AccessibleMediaEditor.iss`. `build_release.ps1` now points at the AME spec/installer, the
version-resource env var is `AME_VERSION_FILE` (also read in the `.spec`), the dead `docs/{en,fr}/index.html`
assertion is gone, and the release-notes markers are `AME-RELEASE-NOTES`. The `.gitignore` was de-AMC'd
too, and the Explorer verb in `installer/AccessibleMediaEditor.iss` is now **"Edit with Accessible Media
Editor" / "Éditer avec…"** under its own `AccessibleMediaEditor` registry subkey (deliberately distinct
from AMC's `AccessibleMediaConverter` subkey so both context-menu entries coexist while AMC is still
installed). The `.iss` carries a UTF-8 BOM so Inno Setup renders the accented French message correctly —
keep the BOM if you edit it. **Still not run through an actual PyInstaller + Inno Setup build**, so treat
a real release as unproven until someone runs it. The updater/support/announce backends are **not wired**
in `main.py` yet (no GitHub repo / backend app-id exists for AME — see below).

**Translations** (English source, French shipped):
```powershell
.venv\Scripts\python.exe .\scripts\manage_i18n.py extract
.venv\Scripts\python.exe .\scripts\manage_i18n.py update --lang fr
```
In dev, `core/i18n.py` loads `.po` files directly via `polib` (no need to compile `.mo`). New UI
strings added during the extraction (`main.py`, `ui/host.py`, the editor's "Open file…") are **not
yet in the catalog** — run `extract`/`update` and translate them.

There is no automated test suite. Validation is manual (smoke test + NVDA).

## Architecture

Two layers, mirroring AMC:

- **`main.py`** — entry point: load config, install gettext, `wx.App`, single-instance check, then
  hand off to `EditorHost`, which opens a file into the editor frame.
- **`ui/host.py`** — `EditorHost`: the **standalone glue** that replaces AMC's main window. Owns the
  `settings_store` (defaults + `%APPDATA%\AccessibleMediaEditor\config.json`), opens files
  (`FileProber.analyze` → `MediaMetadata` → `SegmentEditorFrame`), and provides the two export
  callbacks the editor calls: `choose_settings(parent, meta)` (format `SingleChoiceDialog` +
  `SettingsDialog` from `ui/settings_dialog.py`) and `run_export(meta, plan, mode, fmt_key, settings)` — one re-joined file
  (`SegmentExportTask`) or N separate files (`BatchConversionManager` with the plan frozen onto
  `meta.segment_plan`). Both run on a daemon thread behind a modal `_ExportProgress` bar.
- **`ui/segment_editor.py`** — `SegmentEditorFrame(wx.Frame)`: the editor itself (menu bar, central
  segment list, status bar), taken from AMC. It is constructed with `(parent, meta, on_export,
  on_choose_settings, settings_store, on_open_file, on_persist)`. In AME `parent=None` (it is the
  top-level window); `on_open_file`/`on_persist` are the standalone hooks added during extraction.
- **`core/`** — the media engine, all reused from AMC:
  - `segments.py` — **pure** segment model. `Segment`/`SegmentPlan` pave `[0, duration]`; regions
    keep/discard; `kept_regions` merges adjacent keeps; `validate`, `plan_to_dict`/`plan_from_dict`.
  - `audio_player.py` — `AudioPlayer`: on-demand PCM decode via embedded FFmpeg → `sounddevice`
    (PortAudio). **See the golden rule below.**
  - `segment_export.py` — `SegmentExportTask`: one re-joined file (copy = concat demuxer;
    re-encode = `filter_complex` trim/atrim/setpts/concat).
  - `silence.py` — `detect_silences` / `silence_points` (FFmpeg `silencedetect`, for ad-break jumps).
  - `ffmpeg_helpers.py` — **pure**; resolves `bin/ffmpeg.exe` / `bin/ffprobe.exe` via `_MEIPASS`;
    codec-arg builders shared by conversion/merge/segment_export.
  - `probe.py` — `FileProber` / `MediaMetadata` (wraps `ffprobe`). Pulls in `cue.py` + `track_settings.py`.
  - `formatting.py` — output format keys, codec presets, `settings_store` defaults + normalization.
  - `conversion.py`, `batch_manager.py`, `merge.py` — the batch engine (used for the N-file split;
    `ConversionTask(clip=...)` cuts each kept region via `-ss`/`-t`).
  - `i18n.py`, `speech.py`, `logger.py`, `debug_session.py`, `single_instance.py`, `app_info.py`
    — accessible-app scaffolding (gettext, screen-reader speech, config in `%APPDATA%`, version).
  - `episode_parse.py`, `metadata_edit.py` — **dormant AMC carryover** (filename SxxExx parsing +
    file-tag/cover-art field maps). Not wired into the cutter loop, and `metadata_edit.py` still
    imports a `metadata_retag.py` that wasn't carried over. Leave alone until the editor grows tagging.
  - `updater.py` — GitHub-Releases updater (stdlib only): version compare, SHA-256-verified installer
    download, silent Inno install after exit, startup artifact cleanup. **Wired** (see `ui/segment_editor.py`).
  - `support.py`, `announce.py`, `error_report.py` — support-report + startup-announcement client for the
    shared `mathieumartin.ovh` app-backend (`_APP_ID = "ame"`). Bearer loaded from **`_secrets.py`
    (gitignored)** — copy `_secrets.example.py`. Support dialog + announce-at-startup are wired, but the
    features **no-op until the backend is deployed with the `ame` bearer** (`AME_BEARER_SECRET`).

`bin/ffmpeg.exe` and `bin/ffprobe.exe` are **git-tracked** (~100 MB each) — the app is useless
without them and PyInstaller bundles them from `bin/`.

## Critical gotchas (inherited — do not relearn the hard way)

- **Audio engine golden rule.** `AudioPlayer` opens **one** persistent `sounddevice.RawOutputStream`,
  started once and **never stopped/aborted mid-session** (only closed at shutdown). Earlier designs
  that opened/closed or start/stop/abort the stream per play/scrub **segfaulted natively** (PortAudio
  is not thread-safe and churn crashes it). Idle = the stream stays active, underflowing to silence.
  Cancel kills only the **ffmpeg process**, never the stream from another thread.
- **`filter_complex` timecodes must be decimal seconds** (`f"{ms/1000.0:.3f}"`), never `HH:MM:SS` —
  colons are option separators and break the filter parser ("No option name near '00:00.000'").
- **gettext lazy loading.** `core/` modules import before `install_language()` runs, so they cannot
  call `_()` at module level. Use the lazy `_translate()`/`_translatef()` helper inside functions.
- **`accessible_output2`: DLL outputs only.** Its COM outputs (SAPI/JAWS) **segfault under Python
  3.14**; `core/speech.py` uses only the DLL-based outputs. `pywin32` is dead weight.
- **Python 3.14 `_` shadowing.** Never use `_` as a throwaway variable in a function that also calls
  `_()` for gettext — use explicit names or index access.
- **UI thread safety.** All wx calls from worker threads go through `wx.CallAfter()`.
- **Updater versioning (if/when wired).** A pre-release build must carry the `-rcN` suffix in
  `APP_VERSION` (`core/app_info.py`), or the installed rc reports the stable version and the
  update comparison can't tell them apart. `APP_VERSION_WIN` stays purely numeric.

## Accessibility rules (all wxPython UI here)

- Native wx controls only — no owner-drawn/custom-painted widgets.
- Every `wx.TextCtrl` has a `wx.StaticText` label immediately before it in the sizer.
- Errors via `wx.MessageDialog`/`wx.MessageBox` (NVDA reads these automatically).
- Logical tab order; focus starts on content, not the default button.
- Speech feedback via `core/speech.speak(..., interrupt=True)` (accessible_output2, no screen-reader
  dependency).

## Identity / external resources

- App id / config dir: `%APPDATA%\AccessibleMediaEditor` (`core/debug_session.py`).
- Installer `AppId` GUID: `{8EF4AA32-F74A-45FD-85C6-1E6DDC6D42AE}` (fresh, distinct from AMC).
- GitHub repo **exists and is public**: `math65/accessible-media-editor` (default branch `master`).
  `core/app_info.py` points at it, and the **updater is wired** — but there are **no releases yet**, so
  the update check finds nothing and fails silently. Publish a release before relying on it.
- **Support/announce backend**: app-id `ame` is registered in the `app-backend` repo (`config/apps.json`,
  `bearer_env: AME_BEARER_SECRET`). The client is wired (support dialog + announce-at-startup) but stays
  dormant until the server is deployed with the bearer. To activate: push `app-backend`, set
  `AME_BEARER_SECRET` in the server env (Caddy-injected), restart the service — and put the **same** value
  in the client's gitignored `core/_secrets.py` (`SUPPORT_BEARER`).

## Relationship to Accessible Media Converter

**AME has been dissociated from AMC (2026-07-04): AMC's integrated cutter was removed, and AME is
now the standalone cutter/editor.** The transition is done — AME no longer shares a live codebase
with AMC's cutter.

- **No more two-way porting.** Fixes and features in the engine files
  (`segments`/`audio_player`/`segment_export`/`ffmpeg_helpers`/`probe`/`parse_timecode`…) are
  **AME-only**. Do **not** back-port them to AMC — the cutter isn't there anymore.
- The engine files were originally *carried over* from AMC (see **Origin** above), so historical
  code still reads like AMC's, but the two have now diverged for good on the cutter.
- AMC (`C:\Users\mathi\dev\UniversalTranscoder`) remains a separate converter app; nothing in this
  repo modifies it, and it no longer contains the cutter to keep in sync.
