"""Éditeur de segments accessible — « Cut / Split ».

Fenêtre d'édition (``wx.Frame`` — nécessaire pour porter une **barre de menus**)
permettant de découper un média temporel (audio ou vidéo) en régions à garder ou à
jeter, puis d'exporter soit **1 fichier reconcaténé** (les régions jetées, ex. les
pubs, disparaissent), soit **N fichiers séparés** (une sortie par région gardée).

Conçu pour un usage 100 % clavier + NVDA :
- **toutes les actions sont dans la barre de menus** (découvrables, avec accélérateurs) ;
- la zone centrale est une **unique liste de segments** focusable (NVDA lit chaque
  ligne : n°, début, fin, durée, garder/jeter) — pas de champ de boutons à traverser ;
- la position courante est annoncée à la voix (``core.speech.speak``).

Raccourcis (la liste des segments a le focus) :
- Flèches ← / → : reculer / avancer du **pas** courant ; Origine / Fin : début / fin ;
- Ctrl+← / Ctrl+→ : coupe précédente / suivante ;
- **Espace** : Lecture / Stop (Stop revient au point de départ de la lecture) ;
- **Ctrl+Espace** : Pause / Reprise (la pause fige au playhead) ;
- **S / E** : marquer début / fin d'une région à jeter ; **X** : couper ici ;
- **K** : basculer garder / jeter du segment sélectionné ; **Suppr** : retirer une coupe.

L'éditeur **bloque la fenêtre principale** tant qu'il est ouvert (parent désactivé).
Le résultat est renvoyé à l'appelant par le callback ``on_export(meta, plan, mode)``.
"""

import copy
import json
import os
import threading

import wx

from core.speech import speak
from core import segments as segmods
from core.audio_player import AudioPlayer
from core.ffmpeg_helpers import get_ffmpeg_path
from core.silence import detect_silences, silence_points


# Pas de déplacement proposés (libellé, millisecondes).
_STEP_CHOICES = [
    (lambda: _("10 ms"), 10),
    (lambda: _("100 ms"), 100),
    (lambda: _("1 second"), 1000),
    (lambda: _("10 seconds"), 10000),
    (lambda: _("1 minute"), 60000),
]
_DEFAULT_STEP_INDEX = 2  # 1 seconde

EXPORT_MODE_ONE_FILE = "one_file"
EXPORT_MODE_SEPARATE = "separate"


def format_timecode(ms):
    """Millisecondes → 'HH:MM:SS.mmm' (lisible et sans ambiguïté pour NVDA)."""
    ms = max(0, int(round(ms)))
    hours, rem = divmod(ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def parse_timecode(text):
    """Parse 'HH:MM:SS.mmm', 'MM:SS(.mmm)', 'SS(.mmm)' ou un nombre de secondes en
    millisecondes. Retourne None si non interprétable."""
    text = (text or "").strip().replace(',', '.')
    if not text:
        return None
    try:
        if ':' in text:
            parts = text.split(':')
            if len(parts) > 3:
                return None
            parts = [float(p) for p in parts]
            seconds = 0.0
            for part in parts:
                seconds = seconds * 60 + part
            return int(round(seconds * 1000))
        return int(round(float(text) * 1000))
    except (ValueError, TypeError):
        return None


class SegmentEditorFrame(wx.Frame):
    def __init__(self, parent, meta, on_export, on_choose_settings=None, settings_store=None,
                 on_open_file=None, on_persist=None):
        title = _("Accessible Media Editor — {name}").format(name=os.path.basename(meta.full_path))
        super().__init__(parent, title=title, size=(720, 520),
                         style=wx.DEFAULT_FRAME_STYLE)

        self.meta = meta
        self.on_export_cb = on_export
        self.on_choose_settings = on_choose_settings
        # App autonome : rappel pour ouvrir un autre fichier, et pour persister les
        # préférences (l'app d'origine passait par le parent ; ici parent = None).
        self.on_open_file = on_open_file
        self.on_persist = on_persist
        self._settings = settings_store if isinstance(settings_store, dict) else {}
        self.duration_ms = int(round(float(getattr(meta, 'duration', 0) or 0) * 1000))
        self.plan = segmods.new_plan(self.duration_ms)
        self.position_ms = 0
        self.step_ms = _STEP_CHOICES[_DEFAULT_STEP_INDEX][1]
        self._region_start_ms = None
        self._scrub_enabled = False
        # Options d'annonces vocales (mémorisées dans settings_store).
        self._opt_announce_transport = bool(self._settings.get('cutter_announce_transport', True))
        self._opt_announce_position = bool(self._settings.get('cutter_announce_position', True))
        self._play_anchor_ms = 0      # point de départ de la lecture (Stop y revient)
        self._last_playhead_ms = 0    # dernière tête de lecture connue (Pause s'y pose)
        self._preview_audio_index = None  # piste audio écoutée à l'aperçu (None = défaut)
        self._undo_stack = []
        self._redo_stack = []
        self._silences = []          # liste (start_ms, end_ms)
        self._silence_points = []    # milieux des silences (repères de navigation)
        self._silence_ready = False  # détection terminée ?
        self._closed = False
        self._montage_queue = []     # régions gardées restant à jouer (mode montage)
        self._skip_discarded = False # mode montage : la lecture saute les parties jetées
        self._dirty = False          # découpes modifiées depuis dernier export/enregistrement
        self.player = AudioPlayer()

        self._build_menu()
        self._build_ui()
        self._refresh_segment_list(select_index=0)
        self._update_status()
        self._start_silence_detection()

        # L'éditeur est une fenêtre indépendante : il ne bloque PAS la fenêtre
        # principale, pour pouvoir lancer un export (progression côté fenêtre
        # principale) tout en gardant l'éditeur ouvert et continuer à ajuster.
        self.Bind(wx.EVT_CHAR_HOOK, self.on_char_hook)
        self.Bind(wx.EVT_CLOSE, self.on_close)
        self.CentreOnParent()
        self.Show()
        wx.CallAfter(self.list_ctrl.SetFocus)

    # ------------------------------------------------------------------ menus
    def _build_menu(self):
        bar = wx.MenuBar()

        m_file = wx.Menu()
        # App autonome : ouvrir un autre fichier média dans l'éditeur.
        if callable(self.on_open_file):
            self._append(m_file, _("Open file...") + "\tCtrl+Shift+O",
                         lambda e: self.on_open_file())
            m_file.AppendSeparator()
        self._append(m_file, _("Export as one file (remove discarded segments)") + "\tCtrl+E",
                     lambda e: self._request_export(EXPORT_MODE_ONE_FILE))
        self._append(m_file, _("Export as separate files (one per kept region)") + "\tCtrl+Shift+E",
                     lambda e: self._request_export(EXPORT_MODE_SEPARATE))
        m_file.AppendSeparator()
        self._append(m_file, _("Save project...") + "\tCtrl+S", lambda e: self._save_project())
        self._append(m_file, _("Open project...") + "\tCtrl+O", lambda e: self._open_project())
        m_file.AppendSeparator()
        self._append(m_file, _("Close") + "\tCtrl+W", lambda e: self.Close())
        bar.Append(m_file, _("&File"))

        m_play = wx.Menu()
        # Espace / Ctrl+Espace : gérés par on_char_hook (indiqués en libellé, sans
        # accélérateur, pour ne pas entrer en conflit avec la liste / le hook).
        self._append(m_play, _("Play / Stop") + "  (Space)", lambda e: self._toggle_play())
        self._append(m_play, _("Pause / Resume") + "  (Ctrl+Space)", lambda e: self._toggle_pause())
        self._append(m_play, _("Play current segment"), lambda e: self._play_current_segment())
        self.item_skip = m_play.AppendCheckItem(
            wx.ID_ANY, _("Montage mode: skip discarded parts") + "  (M)")
        self.Bind(wx.EVT_MENU, lambda e: self._set_skip_mode(self.item_skip.IsChecked()), self.item_skip)
        self._append(m_play, _("Verify the cut (real export join)") + "  (V)", lambda e: self._verify_cut())
        m_play.AppendSeparator()
        self.item_scrub = m_play.AppendCheckItem(wx.ID_ANY, _("Scrub on move (audio preview)"))
        self.Bind(wx.EVT_MENU, self.on_scrub_toggle, self.item_scrub)

        # Choix de la piste audio écoutée à l'aperçu (vidéos multipistes : VO/VF,
        # audiodescription…). N'affecte que l'écoute, pas l'export.
        audio_tracks = list(getattr(self.meta, 'audio_tracks', []) or [])
        if len(audio_tracks) > 1:
            m_track = wx.Menu()
            item_default = m_track.AppendRadioItem(wx.ID_ANY, _("Default track"))
            item_default.Check(True)
            self.Bind(wx.EVT_MENU, lambda e: self._select_preview_track(None), item_default)
            for ordinal, track in enumerate(audio_tracks):
                item = m_track.AppendRadioItem(wx.ID_ANY, self._audio_track_label(ordinal, track))
                self.Bind(wx.EVT_MENU, lambda e, n=ordinal: self._select_preview_track(n), item)
            m_play.AppendSubMenu(m_track, _("Preview audio track"))
        bar.Append(m_play, _("&Playback"))

        m_nav = wx.Menu()
        self._append(m_nav, _("Backward") + "  (Left)", lambda e: self._seek_to(self.position_ms - self.step_ms))
        self._append(m_nav, _("Forward") + "  (Right)", lambda e: self._seek_to(self.position_ms + self.step_ms))
        self._append(m_nav, _("Previous cut") + "  (Ctrl+Left)", lambda e: self._seek_to(self._prev_boundary()))
        self._append(m_nav, _("Next cut") + "  (Ctrl+Right)", lambda e: self._seek_to(self._next_boundary()))
        self._append(m_nav, _("Go to start") + "  (Home)", lambda e: self._seek_to(0))
        self._append(m_nav, _("Go to end") + "  (End)", lambda e: self._seek_to(self.duration_ms))
        self._append(m_nav, _("Go to position...") + "\tCtrl+G", lambda e: self._do_goto())
        m_nav.AppendSeparator()
        self._append(m_nav, _("Previous silence") + "  (Alt+Left)", lambda e: self._go_silence(-1))
        self._append(m_nav, _("Next silence") + "  (Alt+Right)", lambda e: self._go_silence(+1))
        m_nav.AppendSeparator()
        m_step = wx.Menu()
        self._append(m_step, _("Finer step") + "  (-)", lambda e: self._change_step(-1))
        self._append(m_step, _("Coarser step") + "  (+)", lambda e: self._change_step(+1))
        m_step.AppendSeparator()
        self._step_items = []
        for index, (label, _ms) in enumerate(_STEP_CHOICES):
            item = m_step.AppendRadioItem(wx.ID_ANY, label())
            if index == _DEFAULT_STEP_INDEX:
                item.Check(True)
            self.Bind(wx.EVT_MENU, lambda e, i=index: self._set_step(i), item)
            self._step_items.append(item)
        m_nav.AppendSubMenu(m_step, _("Step"))
        bar.Append(m_nav, _("&Navigation"))

        m_edit = wx.Menu()
        self._append(m_edit, _("Undo") + "\tCtrl+Z", lambda e: self._undo())
        self._append(m_edit, _("Redo") + "\tCtrl+Y", lambda e: self._redo())
        m_edit.AppendSeparator()
        self._append(m_edit, _("Mark region start") + "  (S)", lambda e: self._mark_start())
        self._append(m_edit, _("Mark region end") + "  (E)", lambda e: self._mark_end())
        self._append(m_edit, _("Add a cut here") + "  (X)", lambda e: self._cut_here())
        self._append(m_edit, _("Keep / Discard segment") + "  (K)", lambda e: self._toggle_selected_keep())
        self._append(m_edit, _("Merge with next segment (remove cut)") + "  (Del)",
                     lambda e: self._remove_selected_boundary())
        m_edit.AppendSeparator()
        self._append(m_edit, _("Move segment start to current position"),
                     lambda e: self._set_selected_boundary(start=True))
        self._append(m_edit, _("Move segment end to current position"),
                     lambda e: self._set_selected_boundary(start=False))
        bar.Append(m_edit, _("&Edit"))

        m_opt = wx.Menu()
        self.item_opt_transport = m_opt.AppendCheckItem(wx.ID_ANY, _("Announce playback actions"))
        self.item_opt_transport.Check(self._opt_announce_transport)
        self.Bind(wx.EVT_MENU, self.on_toggle_announce_transport, self.item_opt_transport)
        self.item_opt_position = m_opt.AppendCheckItem(wx.ID_ANY, _("Announce position when moving"))
        self.item_opt_position.Check(self._opt_announce_position)
        self.Bind(wx.EVT_MENU, self.on_toggle_announce_position, self.item_opt_position)
        bar.Append(m_opt, _("&Options"))

        m_help = wx.Menu()
        self._append(m_help, _("Keyboard shortcuts") + "\tF1", lambda e: self._show_shortcuts())
        bar.Append(m_help, _("&Help"))

        self.SetMenuBar(bar)

    def _append(self, menu, label, handler):
        item = menu.Append(wx.ID_ANY, label)
        self.Bind(wx.EVT_MENU, handler, item)
        return item

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        header = wx.StaticText(panel, label=_("Total duration: {duration}").format(
            duration=format_timecode(self.duration_ms)))
        sizer.Add(header, 0, wx.ALL, 8)

        self.lbl_position = wx.StaticText(panel, label="")
        self.lbl_position.SetName(_("Current position"))
        sizer.Add(self.lbl_position, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.list_ctrl = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self.list_ctrl.SetName(_("Segments"))
        self.list_ctrl.InsertColumn(0, _("#"), width=44)
        self.list_ctrl.InsertColumn(1, _("Start"), width=150)
        self.list_ctrl.InsertColumn(2, _("End"), width=150)
        self.list_ctrl.InsertColumn(3, _("Duration"), width=130)
        self.list_ctrl.InsertColumn(4, _("State"), width=100)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, lambda e: self._go_to_selected_segment())
        sizer.Add(self.list_ctrl, 1, wx.EXPAND | wx.ALL, 8)

        panel.SetSizer(sizer)

        self.CreateStatusBar()
        self.SetStatusText("")

    # ------------------------------------------------------------------ helpers
    def _boundaries(self):
        marks = {0, self.duration_ms}
        for seg in self.plan.segments:
            marks.add(seg.start_ms)
            marks.add(seg.end_ms)
        return sorted(marks)

    def _selected_index(self):
        return self.list_ctrl.GetFirstSelected()

    def _select_row(self, index):
        if 0 <= index < self.list_ctrl.GetItemCount():
            self.list_ctrl.SetItemState(
                index, wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED,
                wx.LIST_STATE_SELECTED | wx.LIST_STATE_FOCUSED)
            self.list_ctrl.EnsureVisible(index)

    def _segment_index_at(self, pos_ms):
        for i, seg in enumerate(self.plan.segments):
            if seg.start_ms <= pos_ms < seg.end_ms:
                return i
        return max(0, len(self.plan.segments) - 1)

    def _refresh_segment_list(self, select_index=None):
        self.list_ctrl.DeleteAllItems()
        for i, seg in enumerate(self.plan.segments):
            row = self.list_ctrl.InsertItem(i, str(i + 1))
            self.list_ctrl.SetItem(row, 1, format_timecode(seg.start_ms))
            self.list_ctrl.SetItem(row, 2, format_timecode(seg.end_ms))
            self.list_ctrl.SetItem(row, 3, format_timecode(seg.duration_ms))
            self.list_ctrl.SetItem(row, 4, _("Keep") if seg.keep else _("Discard"))
        if select_index is not None:
            self._select_row(select_index)

    def _update_status(self):
        self.lbl_position.SetLabel(_("Current position: {time}").format(
            time=format_timecode(self.position_ms)))
        step_label = _STEP_CHOICES[self._step_index()][0]()
        kept = len(segmods.kept_regions(self.plan))
        status = _("Step: {step}   |   Kept regions: {kept}").format(step=step_label, kept=kept)
        if self._silence_ready and self._silence_points:
            status += _("   |   Silences: {count}").format(count=len(self._silence_points))
        self.SetStatusText(status)

    def _step_index(self):
        for i, (_label, ms) in enumerate(_STEP_CHOICES):
            if ms == self.step_ms:
                return i
        return _DEFAULT_STEP_INDEX

    def _announce_position(self):
        if not self._opt_announce_position:
            return
        seg_index = self._segment_index_at(self.position_ms)
        if self.plan.segments:
            seg = self.plan.segments[seg_index]
            state = _("keep") if seg.keep else _("discard")
            speak(_("{time} — segment {index} of {total}, {state}").format(
                time=format_timecode(self.position_ms), index=seg_index + 1,
                total=len(self.plan.segments), state=state))
        else:
            speak(format_timecode(self.position_ms))

    def _seek_to(self, pos_ms, speak_it=True):
        self.position_ms = max(0, min(int(pos_ms), self.duration_ms))
        self._sync_position_label()
        if self._scrub_enabled:
            # Scrub façon REAPER : chaque pas joue un court aperçu (l'audio EST le
            # retour ; pas d'annonce vocale par-dessus).
            self.player.scrub(self.meta.full_path, self.position_ms,
                              audio_index=self._preview_audio_index)
        elif self.player.is_playing():
            # Se déplacer PENDANT la lecture : on continue à jouer depuis la nouvelle
            # position (pas d'arrêt), en respectant le mode (montage ou tout). Le
            # curseur/point de reprise suit le déplacement.
            self._start_playback(self.position_ms)
            if speak_it:
                self._announce_position()
        else:
            if speak_it:
                self._announce_position()

    # ------------------------------------------------------------------ lecture
    def _stop_if_playing(self):
        if self.player.is_playing():
            self.player.stop()

    def _play_from(self, start_ms, end_ms=None):
        start = int(start_ms) if start_ms < self.duration_ms else 0
        self._play_anchor_ms = start
        self._last_playhead_ms = start
        self.player.play(
            self.meta.full_path, start_ms=start,
            end_ms=end_ms if end_ms is not None else self.duration_ms,
            on_position=lambda ms: wx.CallAfter(self._on_playhead, ms),
            on_finished=lambda: wx.CallAfter(self._on_play_finished),
            audio_index=self._preview_audio_index,
        )

    def _say_transport(self, message):
        if self._opt_announce_transport:
            speak(message)

    def _start_playback(self, from_ms):
        """Démarre la lecture selon le mode : montage (saute les parties jetées) ou
        tout, en partant de ``from_ms``."""
        if self._skip_discarded:
            self._start_montage(from_ms)
        else:
            self._play_from(from_ms)

    def _toggle_play(self):
        """Espace : Lecture / Stop. Stop revient à l'ancre (point de départ)."""
        if self.player.is_playing():
            self.player.stop()
            self.position_ms = self._play_anchor_ms
            self._sync_position_label()
            self._say_transport(_("Stopped, back at {time}").format(time=format_timecode(self.position_ms)))
        else:
            self._say_transport(_("Playing the result") if self._skip_discarded else _("Playing"))
            self._start_playback(self.position_ms)

    def _toggle_pause(self):
        """Ctrl+Espace : Pause / Reprise. Pause fige au playhead (le curseur s'y pose)."""
        if self.player.is_playing():
            self.player.stop()
            self.position_ms = max(0, min(int(self._last_playhead_ms), self.duration_ms))
            self._sync_position_label()
            self._say_transport(_("Paused at {time}").format(time=format_timecode(self.position_ms)))
        else:
            self._say_transport(_("Playing the result") if self._skip_discarded else _("Playing"))
            self._start_playback(self.position_ms)

    def _toggle_skip_mode(self):
        self._set_skip_mode(not self._skip_discarded)

    def _set_skip_mode(self, enabled):
        self._skip_discarded = bool(enabled)
        self.item_skip.Check(self._skip_discarded)
        speak(_("Montage mode on (discarded parts skipped)") if self._skip_discarded
              else _("Montage mode off (play everything)"))
        # Bascule en direct pendant la lecture, depuis la position courante entendue.
        if self.player.is_playing():
            self._start_playback(self._last_playhead_ms)

    def _play_current_segment(self):
        index = self._selected_index()
        if index < 0:
            index = self._segment_index_at(self.position_ms)
        if index < 0 or index >= len(self.plan.segments):
            speak(_("No segment selected"))
            return
        seg = self.plan.segments[index]
        self._stop_if_playing()
        self.position_ms = seg.start_ms
        self._sync_position_label()
        self._say_transport(_("Playing segment {index}").format(index=index + 1))
        self._play_from(seg.start_ms, end_ms=seg.end_ms)

    def _audio_track_label(self, ordinal, track):
        summary = ""
        if hasattr(track, 'get_summary'):
            try:
                summary = track.get_summary()
            except Exception:
                summary = ""
        base = _("Track {number}").format(number=ordinal + 1)
        return f"{base}: {summary}" if summary else base

    def _select_preview_track(self, ordinal):
        """Choisit la piste audio écoutée à l'aperçu. Si une lecture est en cours,
        on bascule immédiatement à la position courante avec la nouvelle piste."""
        self._preview_audio_index = ordinal
        if ordinal is None:
            speak(_("Default audio track"))
        else:
            speak(_("Preview audio track {number}").format(number=ordinal + 1))
        if self.player.is_playing():
            self._play_from(self._last_playhead_ms)

    def _sync_position_label(self):
        self.lbl_position.SetLabel(_("Current position: {time}").format(
            time=format_timecode(self.position_ms)))

    def _on_playhead(self, ms):
        # Affiche la tête de lecture EN LECTURE seulement ; le curseur d'édition
        # self.position_ms ne bouge pas, pour que Stop y revienne.
        self._last_playhead_ms = max(0, min(int(ms), self.duration_ms))
        if self.player.is_playing():
            self.lbl_position.SetLabel(_("Current position: {time}").format(
                time=format_timecode(self._last_playhead_ms)))

    def _on_play_finished(self):
        self._sync_position_label()

    def on_scrub_toggle(self, event):
        self._scrub_enabled = self.item_scrub.IsChecked()
        if not self._scrub_enabled:
            self._stop_if_playing()
        speak(_("Scrub on") if self._scrub_enabled else _("Scrub off"))

    # ------------------------------------------------------------------ actions
    def _set_step(self, index):
        self.step_ms = _STEP_CHOICES[index][1]
        if 0 <= index < len(self._step_items):
            self._step_items[index].Check(True)
        self._update_status()
        speak(_("Step: {step}").format(step=_STEP_CHOICES[index][0]()))

    def _change_step(self, delta):
        """Pas plus fin (delta -1) ou plus grand (delta +1), borné à la liste."""
        new_index = max(0, min(self._step_index() + delta, len(_STEP_CHOICES) - 1))
        self._set_step(new_index)

    # -------------------------------------------------------------- options / aide
    def _persist(self):
        # App autonome : le host fournit on_persist. Sinon (intégré), on remonte au
        # parent qui expose _save_config.
        if callable(self.on_persist):
            self.on_persist()
            return
        parent = self.GetParent()
        saver = getattr(parent, '_save_config', None)
        if callable(saver):
            saver()

    def on_toggle_announce_transport(self, event):
        self._opt_announce_transport = self.item_opt_transport.IsChecked()
        self._settings['cutter_announce_transport'] = self._opt_announce_transport
        self._persist()
        speak(_("Playback announcements on") if self._opt_announce_transport
              else _("Playback announcements off"))

    def on_toggle_announce_position(self, event):
        self._opt_announce_position = self.item_opt_position.IsChecked()
        self._settings['cutter_announce_position'] = self._opt_announce_position
        self._persist()
        speak(_("Position announcements on") if self._opt_announce_position
              else _("Position announcements off"))

    def _show_shortcuts(self):
        text = _(
            "Keyboard shortcuts:\n\n"
            "Up / Down: select a segment\n"
            "Left / Right: move by the step\n"
            "Ctrl+Left / Ctrl+Right: previous / next cut\n"
            "Alt+Left / Alt+Right: previous / next silence\n"
            "Home / End: start / end\n"
            "+ / - (or Ctrl+Up / Ctrl+Down): coarser / finer step\n"
            "Space: Play / Stop (Stop returns to the start point)\n"
            "Ctrl+Space: Pause / Resume\n"
            "M: toggle montage mode (playback skips discarded parts)\n"
            "V: verify the cut (hear the real export join)\n"
            "S: mark region start   E: mark region end (creates a discard region)\n"
            "X: add a cut here\n"
            "K: keep / discard the selected segment\n"
            "Delete: remove a cut (merge segments)\n"
            "Ctrl+Z / Ctrl+Y: undo / redo\n"
            "Ctrl+E: export one file   Ctrl+Shift+E: export separate files\n"
            "Ctrl+S / Ctrl+O: save / open project\n"
            "Ctrl+G: go to a position   Ctrl+W: close"
        )
        wx.MessageBox(text, _("Keyboard shortcuts"), wx.ICON_INFORMATION, self)

    def _do_goto(self):
        with wx.TextEntryDialog(self, _("Go to position (HH:MM:SS.mmm):"),
                                _("Go to position")) as dlg:
            if dlg.ShowModal() != wx.ID_OK:
                return
            ms = parse_timecode(dlg.GetValue())
        if ms is None:
            speak(_("Invalid time"))
            wx.MessageBox(_("Please enter a valid time (HH:MM:SS.mmm)."),
                          _("Invalid time"), wx.ICON_WARNING, self)
            return
        self._seek_to(ms)

    def _cut_here(self):
        snapshot = self._snapshot()
        idx = segmods.split_at(self.plan, self.position_ms)
        if idx < 0:
            speak(_("No cut added here"))
            return
        self._commit(snapshot)
        self._refresh_segment_list(select_index=idx)
        self._update_status()
        speak(_("Cut added at {time}").format(time=format_timecode(self.position_ms)))

    def _mark_start(self):
        self._region_start_ms = self.position_ms
        speak(_("Region start marked at {time}").format(time=format_timecode(self.position_ms)))

    def _mark_end(self):
        if self._region_start_ms is None:
            speak(_("Mark a region start first"))
            return
        start = self._region_start_ms
        end = self.position_ms
        if end == start:
            speak(_("Region start and end are identical"))
            return
        snapshot = self._snapshot()
        segmods.mark_region(self.plan, start, end, keep=False)
        self._commit(snapshot)
        self._region_start_ms = None
        lo, hi = (start, end) if start < end else (end, start)
        target = self._segment_index_at(lo + 1)
        self._refresh_segment_list(select_index=target)
        self._update_status()
        speak(_("Discard region created from {start} to {end}").format(
            start=format_timecode(lo), end=format_timecode(hi)))

    def _toggle_selected_keep(self):
        index = self._selected_index()
        if index < 0:
            speak(_("No segment selected"))
            return
        snapshot = self._snapshot()
        segmods.toggle_keep(self.plan, index)
        self._commit(snapshot)
        keep = self.plan.segments[index].keep
        self._refresh_segment_list(select_index=index)
        self._update_status()
        speak(_("Segment {index}: {state}").format(
            index=index + 1, state=_("keep") if keep else _("discard")))

    def _remove_selected_boundary(self):
        index = self._selected_index()
        if index < 0:
            speak(_("No segment selected"))
            return
        if index >= len(self.plan.segments) - 1:
            speak(_("The last segment has no following cut to remove"))
            return
        snapshot = self._snapshot()
        segmods.remove_boundary(self.plan, index)
        self._commit(snapshot)
        self._refresh_segment_list(select_index=index)
        self._update_status()
        speak(_("Cut removed; segments merged"))

    def _go_to_selected_segment(self):
        index = self._selected_index()
        if index < 0 or index >= len(self.plan.segments):
            speak(_("No segment selected"))
            return
        self._seek_to(self.plan.segments[index].start_ms)

    def _set_selected_boundary(self, start):
        """Caler le début (start=True) ou la fin (start=False) du segment
        sélectionné sur la position d'écoute courante."""
        index = self._selected_index()
        if index < 0 or index >= len(self.plan.segments):
            speak(_("No segment selected"))
            return
        snapshot = self._snapshot()
        if start:
            ok = segmods.set_segment_start(self.plan, index, self.position_ms)
        else:
            ok = segmods.set_segment_end(self.plan, index, self.position_ms)
        if not ok:
            speak(_("This boundary cannot be moved here"))
            return
        self._commit(snapshot)
        self._refresh_segment_list(select_index=index)
        self._update_status()
        speak(_("Boundary moved to {time}").format(time=format_timecode(self.position_ms)))

    # ---------------------------------------------------------------- historique
    def _snapshot(self):
        return copy.deepcopy(self.plan.segments)

    def _commit(self, snapshot):
        self._undo_stack.append(snapshot)
        self._redo_stack.clear()
        self._dirty = True

    def _undo(self):
        if not self._undo_stack:
            speak(_("Nothing to undo"))
            return
        self._redo_stack.append(copy.deepcopy(self.plan.segments))
        self.plan.segments = self._undo_stack.pop()
        self._dirty = True
        self._refresh_segment_list(select_index=0)
        self._update_status()
        speak(_("Undone"))

    def _redo(self):
        if not self._redo_stack:
            speak(_("Nothing to redo"))
            return
        self._undo_stack.append(copy.deepcopy(self.plan.segments))
        self.plan.segments = self._redo_stack.pop()
        self._dirty = True
        self._refresh_segment_list(select_index=0)
        self._update_status()
        speak(_("Redone"))

    # ---------------------------------------------------------------- projet
    def _save_project(self):
        stem = os.path.splitext(os.path.basename(self.meta.full_path))[0]
        with wx.FileDialog(self, _("Save project"), defaultFile=f"{stem}.amccut",
                           wildcard=_("Cut project (*.amccut)") + "|*.amccut",
                           style=wx.FD_SAVE | wx.FD_OVERWRITE_PROMPT) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            path = dlg.GetPath()
        data = {
            'app': 'amc-cut', 'version': 1,
            'source': self.meta.full_path,
            'source_name': os.path.basename(self.meta.full_path),
            'duration_ms': self.duration_ms,
            'plan': segmods.plan_to_dict(self.plan),
        }
        try:
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
        except OSError as exc:
            wx.MessageBox(_("Could not save the project.") + f"\n{exc}",
                          _("Error"), wx.ICON_ERROR, self)
            return
        self._dirty = False
        speak(_("Project saved"))
        self._set_frame_status(_("Project saved."))

    def _open_project(self):
        with wx.FileDialog(self, _("Open project"),
                           wildcard=_("Cut project (*.amccut)") + "|*.amccut",
                           style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST) as dlg:
            if dlg.ShowModal() == wx.ID_CANCEL:
                return
            path = dlg.GetPath()
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            wx.MessageBox(_("Could not open the project.") + f"\n{exc}",
                          _("Error"), wx.ICON_ERROR, self)
            return
        source_name = str(data.get('source_name', ''))
        if source_name and source_name != os.path.basename(self.meta.full_path):
            resp = wx.MessageBox(
                _("This project was made for a different file ({name}). Load it anyway?").format(
                    name=source_name),
                _("Open project"), wx.YES_NO | wx.ICON_WARNING, self)
            if resp != wx.YES:
                return
        snapshot = self._snapshot()
        self.plan = segmods.plan_from_dict(data.get('plan', {}), duration_ms=self.duration_ms)
        self._undo_stack.append(snapshot)
        self._redo_stack.clear()
        self._dirty = False
        self._refresh_segment_list(select_index=0)
        self._update_status()
        speak(_("Project opened"))

    def _set_frame_status(self, text):
        try:
            self.SetStatusText(text)
        except Exception:
            pass

    # ---------------------------------------------------------------- silences
    def _start_silence_detection(self):
        path = self.meta.full_path
        ffmpeg_exe = get_ffmpeg_path()

        def worker():
            result = detect_silences(path, ffmpeg_exe)
            wx.CallAfter(self._on_silences, result)

        threading.Thread(target=worker, daemon=True, name='silence-detect').start()

    def _on_silences(self, silences):
        if self._closed:
            return
        self._silences = silences or []
        self._silence_points = silence_points(self._silences)
        self._silence_ready = True
        self._update_status()

    def _go_silence(self, direction):
        if not self._silence_ready:
            speak(_("Detecting silences, please wait"))
            return
        if not self._silence_points:
            speak(_("No silence detected"))
            return
        if direction > 0:
            later = [p for p in self._silence_points if p > self.position_ms]
            target = later[0] if later else None
        else:
            earlier = [p for p in self._silence_points if p < self.position_ms]
            target = earlier[-1] if earlier else None
        if target is None:
            speak(_("No more silence"))
            return
        self._seek_to(target)

    # ---------------------------------------------------------------- vérifier coupe
    def _verify_cut(self):
        """Simule le **raccord réel** de l'export autour du segment sélectionné :
        joue ~2 s de l'audio gardé juste avant la zone jetée, puis (en sautant la
        zone jetée) ~2 s juste après — c.-à-d. exactement ce qu'on entendra dans le
        fichier exporté. Ne déplace pas le curseur d'édition."""
        segs = self.plan.segments
        if not segs:
            return
        index = self._selected_index()
        if index < 0:
            index = self._segment_index_at(self.position_ms)

        if not segs[index].keep:
            # Segment à jeter : étendre à toute la suite contiguë de segments jetés.
            a = index
            while a - 1 >= 0 and not segs[a - 1].keep:
                a -= 1
            b = index
            while b + 1 < len(segs) and not segs[b + 1].keep:
                b += 1
            self._play_join(segs[a].start_ms, segs[b].end_ms)
        elif index + 1 < len(segs) and not segs[index + 1].keep:
            # Gardé suivi d'une zone jetée : raccord après ce segment.
            b = index + 1
            while b + 1 < len(segs) and not segs[b + 1].keep:
                b += 1
            self._play_join(segs[index].end_ms, segs[b].end_ms)
        elif index - 1 >= 0 and not segs[index - 1].keep:
            # Gardé précédé d'une zone jetée : raccord avant ce segment.
            a = index - 1
            while a - 1 >= 0 and not segs[a - 1].keep:
                a -= 1
            self._play_join(segs[a].start_ms, segs[index].start_ms)
        else:
            # Aucune suppression adjacente : l'audio est continu ici (rien à raccorder).
            boundary = segs[index].start_ms if index > 0 else segs[index].end_ms
            start = max(0, boundary - 2000)
            end = min(self.duration_ms, boundary + 2000)
            if end <= start:
                speak(_("No cut to check here"))
                return
            speak(_("No removal here (continuous playback)"))
            self._play_range(start, end)

    def _play_join(self, left_ms, right_ms):
        """Fait entendre le raccord « ...gardé | [saut] | gardé... » : 2 s finissant
        à ``left_ms`` puis 2 s commençant à ``right_ms`` (la partie entre les deux,
        jetée, est sautée)."""
        pre = (max(0, left_ms - 2000), left_ms) if left_ms > 0 else None
        post = (right_ms, min(self.duration_ms, right_ms + 2000)) if right_ms < self.duration_ms else None
        self._say_transport(_("Simulating join: {a} to {b}").format(
            a=format_timecode(left_ms), b=format_timecode(right_ms)))
        if pre and post:
            self._play_anchor_ms = pre[0]
            self._last_playhead_ms = pre[0]
            self.player.play(
                self.meta.full_path, start_ms=pre[0], end_ms=pre[1],
                on_position=lambda ms: wx.CallAfter(self._on_playhead, ms),
                on_finished=lambda: wx.CallAfter(self._play_range, post[0], post[1]),
                audio_index=self._preview_audio_index,
            )
        elif pre:
            self._play_range(pre[0], pre[1])
        elif post:
            self._play_range(post[0], post[1])

    def _play_range(self, start_ms, end_ms):
        self._play_anchor_ms = start_ms
        self._last_playhead_ms = start_ms
        self.player.play(
            self.meta.full_path, start_ms=start_ms, end_ms=end_ms,
            on_position=lambda ms: wx.CallAfter(self._on_playhead, ms),
            on_finished=lambda: wx.CallAfter(self._on_play_finished),
            audio_index=self._preview_audio_index,
        )

    # ---------------------------------------------------------------- montage
    def _start_montage(self, from_ms):
        """Joue le RÉSULTAT (régions gardées, parties jetées sautées) en partant de
        ``from_ms`` : la 1re région est rognée à la position courante. L'ancre (point
        de retour du Stop) reste ``from_ms``."""
        from_ms = max(0, min(int(from_ms), self.duration_ms))
        regions = [(max(s, from_ms), e) for (s, e) in segmods.kept_regions(self.plan) if e > from_ms]
        if not regions:
            speak(_("Nothing to play (all discarded)"))
            return
        self._montage_queue = regions
        self._play_anchor_ms = from_ms
        self._last_playhead_ms = from_ms
        self._play_next_montage_region()

    def _play_next_montage_region(self):
        if not self._montage_queue:
            self._on_play_finished()
            return
        start_ms, end_ms = self._montage_queue.pop(0)
        self._last_playhead_ms = start_ms  # affichage : le playhead saute au début de la région
        self.player.play(
            self.meta.full_path, start_ms=start_ms, end_ms=end_ms,
            on_position=lambda ms: wx.CallAfter(self._on_playhead, ms),
            on_finished=lambda: wx.CallAfter(self._play_next_montage_region),
            audio_index=self._preview_audio_index,
        )

    # ------------------------------------------------------------------ navigation bornes
    def _prev_boundary(self):
        prev = 0
        for mark in self._boundaries():
            if mark < self.position_ms:
                prev = mark
            else:
                break
        return prev

    def _next_boundary(self):
        for mark in self._boundaries():
            if mark > self.position_ms:
                return mark
        return self.duration_ms

    # ------------------------------------------------------------------ export / close
    def _request_export(self, mode):
        if not callable(self.on_export_cb):
            return
        error = segmods.validate(self.plan)
        if error:
            speak(error)
            wx.MessageBox(error, _("Cannot export"), wx.ICON_WARNING, self)
            return
        # Choix du format/qualité (parenté à l'éditeur). Annuler → on ne fait rien.
        fmt_key = settings = None
        if callable(self.on_choose_settings):
            fmt_key, settings = self.on_choose_settings(self, self.meta)
            if fmt_key is None:
                return
        self.player.stop()
        # L'export se lance SANS fermer l'éditeur : la fenêtre reste ouverte pour
        # ré-exporter ou continuer à ajuster. La progression s'affiche côté fenêtre
        # principale.
        launched = self.on_export_cb(self.meta, self.plan, mode, fmt_key, settings)
        if launched:
            self._dirty = False
            speak(_("Export started"))

    def on_close(self, event):
        # Confirmation si des découpes non exportées / non enregistrées seraient
        # perdues (Alt+F4, Ctrl+W…).
        if self._dirty:
            resp = wx.MessageBox(
                _("Close without saving? Your cuts will be lost."),
                _("Cut / Split"), wx.YES_NO | wx.ICON_WARNING, self)
            if resp != wx.YES:
                if hasattr(event, 'CanVeto') and event.CanVeto():
                    event.Veto()
                return

        self._closed = True
        self.player.shutdown()  # ferme le flux + termine le thread moteur
        self.Destroy()

    # ------------------------------------------------------------------ clavier
    def on_char_hook(self, event):
        key = event.GetKeyCode()
        ctrl = event.ControlDown()
        alt = event.AltDown()

        # Échap ne doit RIEN faire ici (ne pas fermer l'éditeur par mégarde).
        if key == wx.WXK_ESCAPE:
            return

        # Alt+Gauche/Droite = silence précédent/suivant. Les autres combinaisons Alt
        # (Alt+lettre) sont laissées à la barre de menus.
        if alt and key == wx.WXK_LEFT:
            self._go_silence(-1); return
        if alt and key == wx.WXK_RIGHT:
            self._go_silence(+1); return
        if alt:
            event.Skip(); return

        # Ctrl+Espace = Pause / Reprise (Ctrl+Espace n'est utilisé par aucun contrôle).
        if key == wx.WXK_SPACE and ctrl:
            self._toggle_pause(); return
        # Espace = Lecture / Stop (on remplace le rôle natif de la liste).
        if key == wx.WXK_SPACE and not ctrl:
            self._toggle_play(); return

        # Changer le pas : Ctrl+Haut/Bas ou +/- (pavé numérique inclus). Haut/+ =
        # pas plus grand ; Bas/- = pas plus fin. (Haut/Bas seuls restent à la liste.)
        if ctrl and key == wx.WXK_UP:
            self._change_step(+1); return
        if ctrl and key == wx.WXK_DOWN:
            self._change_step(-1); return
        if key in (wx.WXK_NUMPAD_ADD, ord('+')):
            self._change_step(+1); return
        if key in (wx.WXK_NUMPAD_SUBTRACT, ord('-')):
            self._change_step(-1); return

        # Ctrl+Z / Ctrl+Y (undo/redo) sont des accélérateurs de menu → on laisse
        # passer. Ici on ne traite que les lettres SANS Ctrl.
        if not ctrl:
            if key in (ord('S'), ord('s')):
                self._mark_start(); return
            if key in (ord('E'), ord('e')):
                self._mark_end(); return
            if key in (ord('X'), ord('x')):
                self._cut_here(); return
            if key in (ord('K'), ord('k')):
                self._toggle_selected_keep(); return
            if key in (ord('V'), ord('v')):
                self._verify_cut(); return
            if key in (ord('M'), ord('m')):
                self._toggle_skip_mode(); return
        if key == wx.WXK_DELETE:
            self._remove_selected_boundary(); return

        # Navigation temporelle (Haut/Bas restent à la liste pour choisir un segment).
        if key == wx.WXK_LEFT:
            self._seek_to(self._prev_boundary() if event.ControlDown()
                          else self.position_ms - self.step_ms)
            return
        if key == wx.WXK_RIGHT:
            self._seek_to(self._next_boundary() if event.ControlDown()
                          else self.position_ms + self.step_ms)
            return
        if key == wx.WXK_HOME:
            self._seek_to(0); return
        if key == wx.WXK_END:
            self._seek_to(self.duration_ms); return

        event.Skip()
