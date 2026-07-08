"""Contrôleur de l'application autonome « Accessible Media Editor ».

Dans l'app d'origine (Accessible Media Converter), l'éditeur de segments était
lancé comme fenêtre enfant, et la fenêtre principale fournissait les callbacks
d'export. Ici, **l'éditeur EST l'application** : ce host tient lieu de fenêtre
principale invisible — il ouvre un fichier dans un ``SegmentEditorFrame`` et pilote
l'export (choix format/qualité, un fichier reconcaténé ou fichiers séparés) avec une
barre de progression modale.
"""

import json
import os
import threading

import wx

from core.batch_manager import BatchConversionManager
from core.conversion import get_output_extension
from core.debug_session import load_raw_config, save_raw_config
from core.formatting import (
    AUDIO_OUTPUT_FORMAT_KEYS,
    VIDEO_OUTPUT_FORMAT_KEYS,
    build_default_settings_store,
    build_format_label,
)
from core.probe import FileProber
from core.segment_export import SegmentExportTask
from core.segments import kept_regions
from ui.segment_editor import EXPORT_MODE_SEPARATE, SegmentEditorFrame
from ui.settings_dialog import SettingsDialog

# Extensions média acceptées à l'ouverture (audio + vidéo temporels ; pas d'images).
_MEDIA_WILDCARD = (
    "*.mp3;*.m4a;*.aac;*.wav;*.flac;*.alac;*.ogg;*.opus;*.wma;*.mp2;*.ac3;*.eac3;"
    "*.dts;*.mka;*.amr;*.m4b;*.mp4;*.mkv;*.mov;*.avi;*.webm;*.ts;*.m2ts;*.mts;"
    "*.mpg;*.mpeg;*.vob;*.m4v;*.3gp;*.3g2;*.flv;*.ogv"
)


class _ExportProgress:
    """Barre de progression modale pilotée depuis le thread principal.

    Le worker (thread daemon ou BatchConversionManager) pousse la progression via
    ``wx.CallAfter(set_progress, pct)`` et signale la fin via ``finish(ok, message)``.
    Le sondage (``_poll``) tourne sur le thread principal — seul endroit où l'on
    touche ``wx.ProgressDialog``.
    """

    def __init__(self, parent, title, on_cancel):
        self.pct = 0
        self.done = False
        self.result = None
        self._on_cancel = on_cancel
        self._cancel_sent = False
        self.dlg = wx.ProgressDialog(
            title, _("Exporting..."), maximum=100, parent=parent,
            style=wx.PD_APP_MODAL | wx.PD_CAN_ABORT | wx.PD_ELAPSED_TIME)
        wx.CallLater(120, self._poll)

    def set_progress(self, pct):
        try:
            self.pct = max(0, min(100, int(pct)))
        except (TypeError, ValueError):
            pass

    def finish(self, success, message):
        self.result = (bool(success), message)
        self.done = True

    def _poll(self):
        if self.done:
            self.dlg.Destroy()
            success, message = self.result or (False, "")
            if message:
                wx.MessageBox(message, _("Export"),
                              wx.ICON_INFORMATION if success else wx.ICON_ERROR)
            return
        cont, _skip = self.dlg.Update(self.pct)
        if not cont and not self._cancel_sent:
            self._cancel_sent = True
            self.dlg.Update(self.pct, _("Stopping..."))
            if callable(self._on_cancel):
                self._on_cancel()
        wx.CallLater(120, self._poll)


class EditorHost:
    def __init__(self):
        self.settings_store = build_default_settings_store()
        try:
            self.settings_store.update(load_raw_config() or {})
        except Exception:  # noqa: BLE001
            pass
        self.prober = FileProber()
        self.editor = None
        self.welcome = None      # écran d'accueil « aucun fichier » (fenêtre racine)
        self._replacing = False  # True pendant le remplacement de l'éditeur (ne pas ré-accueillir)

    # -------------------------------------------------------------- persistence
    def _save_config(self):
        try:
            save_raw_config(self.settings_store)
        except Exception:  # noqa: BLE001
            pass

    # ---------------------------------------------------------- welcome screen
    def show_welcome(self):
        """Affiche (ou crée) l'écran d'accueil « aucun fichier ». C'est la fenêtre
        racine qui maintient la boucle wx en vie quand aucun éditeur n'est ouvert."""
        from ui.welcome_frame import WelcomeFrame
        if self.welcome is None:
            self.welcome = WelcomeFrame(on_open=self.open_file, on_paste=self.paste_open)
        self.welcome.Show()
        self.welcome.Raise()
        wx.CallAfter(self.welcome.btn_open.SetFocus)
        return self.welcome

    def _on_editor_closed(self):
        """Appelé quand l'éditeur se ferme. On revient à l'accueil, sauf si c'est un
        remplacement par un autre fichier (load_path gère alors l'éditeur lui-même)."""
        if self._replacing:
            return
        self.editor = None
        self.show_welcome()

    def paste_open(self):
        """Ctrl+V depuis l'accueil : ouvre le fichier copié dans l'Explorateur, ou le
        chemin présent dans le presse-papier."""
        path = self._clipboard_path()
        if not path:
            wx.MessageBox(_("The clipboard does not contain a file or a valid path."),
                          _("Paste"), wx.ICON_INFORMATION)
            return False
        if not os.path.isfile(path):
            wx.MessageBox(
                _("This path does not point to an existing file:\n{path}").format(path=path),
                _("Paste"), wx.ICON_WARNING)
            return False
        return self.load_path(path)

    @staticmethod
    def _clipboard_path():
        """Chemin de fichier depuis le presse-papier : un fichier copié (CF_HDROP de
        l'Explorateur) en priorité, sinon un chemin en texte. None si rien d'exploitable."""
        if not wx.TheClipboard.Open():
            return None
        try:
            files = wx.FileDataObject()
            if wx.TheClipboard.GetData(files):
                names = files.GetFilenames()
                if names:
                    return names[0]
            text = wx.TextDataObject()
            if wx.TheClipboard.GetData(text):
                value = (text.GetText() or "").strip().strip('"')
                if value:
                    return value
        finally:
            wx.TheClipboard.Close()
        return None

    # ------------------------------------------------------------- open a file
    def open_file(self):
        """Menu « Ouvrir un fichier… » (depuis l'accueil ou l'éditeur)."""
        wildcard = (
            _("Media files") + f" ({_MEDIA_WILDCARD})|{_MEDIA_WILDCARD}|"
            + _("Cut project (*.amccut)") + "|*.amccut|"
            + _("All files") + " (*.*)|*.*"
        )
        parent = self.editor or self.welcome
        with wx.FileDialog(parent, _("Open a media file"), wildcard=wildcard,
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return False
            path = dlg.GetPath()
        return self.load_path(path)

    def load_path(self, path):
        """Analyse le fichier et (re)crée l'éditeur dessus. Retourne True si ouvert.
        Un fichier projet ``.amccut`` ouvre directement le média qu'il référence."""
        if os.path.splitext(path)[1].lower() == '.amccut':
            return self.load_project(path)
        meta = self.prober.analyze(path)
        if getattr(meta, 'is_image', False) or getattr(meta, 'cue_sheet', None) is not None:
            wx.MessageBox(_("This kind of file cannot be edited here."),
                          _("Cannot open"), wx.ICON_WARNING)
            return False
        if not getattr(meta, 'duration', 0):
            wx.MessageBox(_("This file has no known duration and cannot be edited."),
                          _("Cannot open"), wx.ICON_WARNING)
            return False

        # Remplacer l'éditeur courant : Close() déclenche la garde « non enregistré ».
        # _replacing empêche _on_editor_closed de rebasculer vers l'accueil entre-temps.
        if self.editor is not None:
            self._replacing = True
            try:
                if not self.editor.Close():
                    return False  # l'utilisateur a annulé (découpes non enregistrées)
            finally:
                self._replacing = False
            self.editor = None

        self.editor = SegmentEditorFrame(
            None, meta, self.run_export,
            on_choose_settings=self.choose_settings,
            settings_store=self.settings_store,
            on_open_file=self.open_file,
            on_persist=self._save_config,
            on_closed=self._on_editor_closed,
        )
        # L'accueil laisse la place à l'éditeur (il sera réaffiché à la fermeture).
        if self.welcome is not None:
            self.welcome.Hide()
        return True

    def load_project(self, project_path):
        """Ouvre un projet ``.amccut`` directement : lit le média qu'il référence,
        l'ouvre dans l'éditeur, puis applique le plan de découpe. Évite d'avoir à
        ouvrir le média à la main avant de charger le projet."""
        try:
            with open(project_path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            wx.MessageBox(_("Could not open the project.") + f"\n{exc}",
                          _("Cannot open"), wx.ICON_WARNING)
            return False
        source = str(data.get('source', '') or '')
        if not source or not os.path.isfile(source):
            wx.MessageBox(
                _("The media file for this project was not found:\n{name}").format(
                    name=data.get('source_name') or source or _("(unknown)")),
                _("Cannot open"), wx.ICON_WARNING)
            return False
        if not self.load_path(source):
            return False
        self.editor.apply_project_plan(data.get('plan', {}))
        return True

    # --------------------------------------------------- format/quality dialog
    def choose_settings(self, parent, meta):
        """Fenêtre format + qualité (pré-remplie). Retourne (fmt_key, settings) ou
        (None, None) si annulé. ``parent`` = l'éditeur."""
        is_video = bool(getattr(meta, 'has_video', False))
        context = 'video' if is_video else 'audio'
        fmt_keys = list(VIDEO_OUTPUT_FORMAT_KEYS if is_video else AUDIO_OUTPUT_FORMAT_KEYS)
        labels = [build_format_label(key, context=context) for key in fmt_keys]
        last_key = self.settings_store.get('last_format_video' if is_video else 'last_format_audio')
        preselect = fmt_keys.index(last_key) if last_key in fmt_keys else 0

        fmt_dlg = wx.SingleChoiceDialog(parent, _("Output format for the export:"),
                                        _("Export settings"), labels)
        fmt_dlg.SetSelection(preselect)
        if fmt_dlg.ShowModal() != wx.ID_OK:
            fmt_dlg.Destroy()
            return None, None
        fmt_key = fmt_keys[fmt_dlg.GetSelection()]
        fmt_dlg.Destroy()

        clean = build_format_label(fmt_key, context=context)
        input_ac = getattr(meta, 'audio_codec', '') or ""
        current_saved = self.settings_store.get(fmt_key, {})
        dlg = SettingsDialog(parent, clean, is_video, input_ac, current_saved, fmt_key)
        if dlg.ShowModal() != wx.ID_OK:
            dlg.Destroy()
            return None, None
        raw = dict(dlg.get_settings())
        dlg.Destroy()

        # Mémoriser le format + réglages choisis pour la prochaine fois.
        self.settings_store[fmt_key] = dict(raw)
        self.settings_store['last_format_video' if is_video else 'last_format_audio'] = fmt_key
        self._save_config()

        settings = dict(raw)
        settings['ffmpeg_threads'] = self.settings_store.get('ffmpeg_threads', 'auto')
        settings['preserve_metadata'] = self.settings_store.get('preserve_metadata', False)
        settings['m4b_chapter_naming'] = self.settings_store.get('m4b_chapter_naming', 'title_or_number')
        # Préférence globale « copie exacte » injectée dans le dict par-export → visible
        # côté SegmentExportTask (1 fichier) comme côté batch (N fichiers).
        settings['cutter_smart_cut'] = self.settings_store.get('cutter_smart_cut', False)
        return fmt_key, settings

    # ------------------------------------------------------------------ export
    def run_export(self, meta, plan, mode, fmt_key, settings):
        if not fmt_key:
            return False
        if mode == EXPORT_MODE_SEPARATE:
            return self._export_separate(meta, plan, fmt_key, settings)
        return self._export_one_file(meta, plan, fmt_key, settings)

    def _export_one_file(self, meta, plan, fmt_key, settings):
        regions = kept_regions(plan)
        if not regions:
            return False
        ext = get_output_extension(fmt_key)
        stem = os.path.splitext(os.path.basename(meta.full_path))[0]
        default_name = f"{stem} (cut).{ext}"
        default_dir = os.path.dirname(meta.full_path) or os.getcwd()
        with wx.FileDialog(self.editor, _("Save cut file"), defaultDir=default_dir,
                           defaultFile=default_name,
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return False
            output_path = dlg.GetPath()

        # Forcer la bonne extension si l'utilisateur l'a retirée (ex. « test ») : sans
        # elle, FFmpeg ne peut pas déduire le conteneur et l'export échoue.
        if os.path.splitext(output_path)[1].lower() != f".{ext}".lower():
            output_path += f".{ext}"

        task = SegmentExportTask(meta, regions, fmt_key, settings, output_path)
        stop_flag = {'v': False}

        def _cancel():
            stop_flag['v'] = True
            task.stop()

        prog = _ExportProgress(self.editor, _("Export"), on_cancel=_cancel)

        def _worker():
            ok = True
            message = _("Export complete: {path}").format(path=output_path)
            try:
                task.run(
                    progress_callback=lambda pct: wx.CallAfter(prog.set_progress, pct),
                    stop_check_callback=lambda: stop_flag['v'],
                )
            except Exception as exc:  # noqa: BLE001 - remonté à l'utilisateur
                ok = False
                message = _("Export failed: {error}").format(error=str(exc))
            if stop_flag['v'] and ok:
                message = _("Export stopped.")
            wx.CallAfter(prog.finish, ok and not stop_flag['v'], message)

        threading.Thread(target=_worker, daemon=True).start()
        return True

    def _export_separate(self, meta, plan, fmt_key, settings):
        regions = kept_regions(plan)
        if not regions:
            return False
        default_dir = os.path.dirname(meta.full_path) or os.getcwd()
        with wx.DirDialog(self.editor, _("Select output folder for the pieces"),
                          defaultPath=default_dir) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return False
            out_dir = dlg.GetPath()

        def _on_update(summary):
            wx.CallAfter(prog.set_progress, summary.get('overall_progress', 0))

        def _on_complete(summary):
            stopped = summary.get('user_stopped')
            errors = summary.get('error', 0)
            done = summary.get('done', 0)
            total = summary.get('total', 0)
            if stopped:
                message = _("Export stopped.")
            elif errors:
                message = _("Finished with errors: {done}/{total} file(s) created.").format(
                    done=done, total=total)
            else:
                message = _("Export complete: {n} file(s) created in {dir}").format(
                    n=done, dir=out_dir)
            wx.CallAfter(prog.finish, (not errors and not stopped), message)

        meta.segment_plan = plan
        try:
            manager = BatchConversionManager(
                [meta], fmt_key, settings, output_dir=out_dir,
                max_concurrent=self.settings_store.get('max_concurrent_jobs', 2),
                output_policy=self.settings_store.get('existing_output_policy', 'rename'),
                continue_on_error=self.settings_store.get('continue_on_error', True),
                on_batch_update=_on_update,
                on_batch_complete=_on_complete,
            )
        finally:
            # Le plan est figé dans les jobs à la construction ; on l'efface du meta.
            meta.segment_plan = None

        prog = _ExportProgress(self.editor, _("Export"), on_cancel=manager.stop)
        manager.start()
        return True
