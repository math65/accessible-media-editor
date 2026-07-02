"""Application preferences dialog (app-level settings).

Accessible, native wx controls only: every control has a StaticText label right
before it, focus starts on the first control, OK/Cancel with logical tab order.
Reads/writes the editor frame's settings_store (``parent._settings``) and
persists via ``parent._persist()``. Speech-announcement toggles are applied live
on the parent so they take effect without a restart; the UI language change only
takes effect on the next launch.
"""
import wx

from core.formatting import (
    MAX_CONCURRENT_JOBS,
    MIN_CONCURRENT_JOBS,
    VALID_EXISTING_OUTPUT_POLICIES,
    get_ffmpeg_thread_values,
)
from core.i18n import AUTO_LANGUAGE_CODE, SUPPORTED_LANGUAGE_CODES

_POLICY_LABEL_MSGIDS = {
    "rename": "Rename automatically",
    "overwrite": "Overwrite existing file",
    "skip": "Skip existing file",
}
_LANGUAGE_LABEL_MSGIDS = {
    "auto": "Automatic",
    "fr": "French",
    "en": "English",
}


class PreferencesDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title=_("Preferences"), size=(520, 560))
        self.SetName(_("Preferences"))
        self.parent_window = parent
        self._settings = parent._settings

        # Ordered code lists paired with their control's item index.
        self._policy_codes = list(VALID_EXISTING_OUTPUT_POLICIES)
        self._thread_values = ["auto"] + [str(v) for v in get_ffmpeg_thread_values()]
        self._lang_codes = [AUTO_LANGUAGE_CODE, *SUPPORTED_LANGUAGE_CODES]

        self._build_ui()
        self._load_values()
        self.Centre()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        root.Add(self._build_updates_box(panel), 0, wx.EXPAND | wx.ALL, 10)
        root.Add(self._build_export_box(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self._build_speech_box(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)
        root.Add(self._build_language_box(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        buttons = wx.StdDialogButtonSizer()
        self.btn_ok = wx.Button(panel, wx.ID_OK, _("OK"))
        self.btn_ok.SetDefault()
        self.btn_cancel = wx.Button(panel, wx.ID_CANCEL, _("Cancel"))
        buttons.AddButton(self.btn_ok)
        buttons.AddButton(self.btn_cancel)
        buttons.Realize()
        root.Add(buttons, 0, wx.EXPAND | wx.ALL, 10)

        panel.SetSizer(root)
        self.Bind(wx.EVT_BUTTON, self.on_ok, self.btn_ok)

    def _build_updates_box(self, panel):
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Updates"))
        self.cb_check_updates = wx.CheckBox(box.GetStaticBox(), label=_("Check for updates on startup"))
        self.cb_prereleases = wx.CheckBox(
            box.GetStaticBox(), label=_("Include pre-releases (rc / beta) in the update check"))
        box.Add(self.cb_check_updates, 0, wx.ALL, 6)
        box.Add(self.cb_prereleases, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        return box

    def _build_export_box(self, panel):
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Export"))
        parent = box.GetStaticBox()

        grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        grid.AddGrowableCol(1, 1)

        lbl_policy = wx.StaticText(parent, label=_("If the output file already exists"))
        self.choice_policy = wx.Choice(
            parent, choices=[_(_POLICY_LABEL_MSGIDS[c]) for c in self._policy_codes])
        self.choice_policy.SetName(_("If the output file already exists"))

        lbl_jobs = wx.StaticText(parent, label=_("Maximum simultaneous conversions"))
        self.spin_jobs = wx.SpinCtrl(
            parent, min=MIN_CONCURRENT_JOBS, max=MAX_CONCURRENT_JOBS)
        self.spin_jobs.SetName(_("Maximum simultaneous conversions"))

        lbl_threads = wx.StaticText(parent, label=_("FFmpeg threads"))
        thread_labels = [_("Automatic")] + self._thread_values[1:]
        self.choice_threads = wx.Choice(parent, choices=thread_labels)
        self.choice_threads.SetName(_("FFmpeg threads"))

        for lbl, ctrl in (
            (lbl_policy, self.choice_policy),
            (lbl_jobs, self.spin_jobs),
            (lbl_threads, self.choice_threads),
        ):
            grid.Add(lbl, 0, wx.ALIGN_CENTER_VERTICAL)
            grid.Add(ctrl, 0, wx.EXPAND)
        box.Add(grid, 0, wx.EXPAND | wx.ALL, 6)

        self.cb_continue = wx.CheckBox(parent, label=_("Continue despite errors"))
        self.cb_open_folder = wx.CheckBox(parent, label=_("Open the output folder after export"))
        box.Add(self.cb_continue, 0, wx.LEFT | wx.RIGHT | wx.TOP, 6)
        box.Add(self.cb_open_folder, 0, wx.ALL, 6)
        return box

    def _build_speech_box(self, panel):
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Speech"))
        parent = box.GetStaticBox()
        self.cb_ann_transport = wx.CheckBox(parent, label=_("Announce playback actions"))
        self.cb_ann_position = wx.CheckBox(parent, label=_("Announce position when moving"))
        box.Add(self.cb_ann_transport, 0, wx.ALL, 6)
        box.Add(self.cb_ann_position, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        return box

    def _build_language_box(self, panel):
        box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Language"))
        parent = box.GetStaticBox()

        row = wx.BoxSizer(wx.HORIZONTAL)
        lbl_lang = wx.StaticText(parent, label=_("Interface language"))
        self.choice_lang = wx.Choice(
            parent, choices=[_(_LANGUAGE_LABEL_MSGIDS[c]) for c in self._lang_codes])
        self.choice_lang.SetName(_("Interface language"))
        row.Add(lbl_lang, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 10)
        row.Add(self.choice_lang, 1, wx.EXPAND)
        box.Add(row, 0, wx.EXPAND | wx.ALL, 6)

        note = wx.StaticText(parent, label=_("Applied on the next launch."))
        box.Add(note, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        return box

    # ------------------------------------------------------------ values
    def _load_values(self):
        s = self._settings
        self.cb_check_updates.SetValue(bool(s.get("check_updates_on_startup", True)))
        self.cb_prereleases.SetValue(bool(s.get("include_prereleases", False)))
        self.cb_continue.SetValue(bool(s.get("continue_on_error", True)))
        self.cb_open_folder.SetValue(bool(s.get("open_output_folder_after_batch", False)))
        self.cb_ann_transport.SetValue(bool(s.get("cutter_announce_transport", True)))
        self.cb_ann_position.SetValue(bool(s.get("cutter_announce_position", True)))

        self.choice_policy.SetSelection(self._index(self._policy_codes, s.get("existing_output_policy", "rename")))
        self.spin_jobs.SetValue(int(s.get("max_concurrent_jobs", MIN_CONCURRENT_JOBS) or MIN_CONCURRENT_JOBS))
        self.choice_threads.SetSelection(self._index(self._thread_values, str(s.get("ffmpeg_threads", "auto"))))
        self.choice_lang.SetSelection(self._index(self._lang_codes, s.get("ui_language", AUTO_LANGUAGE_CODE)))

        self.cb_check_updates.SetFocus()

    @staticmethod
    def _index(codes, value):
        try:
            return codes.index(str(value))
        except ValueError:
            return 0

    def on_ok(self, event):
        s = self._settings
        s["check_updates_on_startup"] = self.cb_check_updates.GetValue()
        s["include_prereleases"] = self.cb_prereleases.GetValue()
        s["existing_output_policy"] = self._policy_codes[self.choice_policy.GetSelection()]
        s["max_concurrent_jobs"] = self.spin_jobs.GetValue()
        s["ffmpeg_threads"] = self._thread_values[self.choice_threads.GetSelection()]
        s["continue_on_error"] = self.cb_continue.GetValue()
        s["open_output_folder_after_batch"] = self.cb_open_folder.GetValue()
        s["cutter_announce_transport"] = self.cb_ann_transport.GetValue()
        s["cutter_announce_position"] = self.cb_ann_position.GetValue()
        s["ui_language"] = self._lang_codes[self.choice_lang.GetSelection()]

        # Speech toggles take effect immediately on the live editor.
        self.parent_window._opt_announce_transport = s["cutter_announce_transport"]
        self.parent_window._opt_announce_position = s["cutter_announce_position"]
        self.parent_window._persist()
        self.EndModal(wx.ID_OK)
