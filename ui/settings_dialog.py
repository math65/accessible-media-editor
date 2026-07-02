import wx

from core.formatting import (
    DEFAULT_FORMAT_SETTINGS,
    IMAGE_OUTPUT_FORMAT_KEYS,
    IMAGE_RESIZE_OPTIONS,
    VALID_VIDEO_ENCODER_PRESETS,
    VALID_VIDEO_PIXEL_FORMATS,
    VALID_VIDEO_PROFILES,
    VIDEO_CONTAINER_FORMAT_KEYS,
    VIDEO_PRESET_PROFILE_SETTINGS,
    build_format_summary,
    build_image_format_summary,
    get_audio_codec_label,
    get_container_audio_codec_options,
    get_matching_video_preset_profile,
)

VIDEO_CRF_PRESET_OPTIONS = (
    (16, "Very High Quality"),
    (18, "High Quality"),
    (20, "Quality"),
    (22, "Balanced Quality"),
    (23, "Balanced - Recommended"),
    (24, "Compact"),
    (26, "More Compact"),
    (28, "Small File"),
    (30, "Very Compact"),
)

VIDEO_PRESET_OPTIONS = (
    ("compatible", "Compatible"),
    ("balanced", "Balanced"),
    ("high_quality", "High Quality"),
    ("small_file", "Small File"),
    ("fast_encode", "Fast Encode"),
)

VIDEO_ENCODER_PRESET_OPTIONS = (
    ("veryfast", "Very Fast"),
    ("fast", "Fast"),
    ("medium", "Medium"),
    ("slow", "Slow"),
)

VIDEO_PROFILE_OPTIONS = (
    ("baseline", "Baseline"),
    ("main", "Main"),
    ("high", "High"),
)

VIDEO_PIXEL_FORMAT_OPTIONS = (
    ("yuv420p", "YUV 4:2:0 (Compatible)"),
    ("yuv444p", "YUV 4:4:4 (Advanced)"),
)


class SettingsDialog(wx.Dialog):
    def __init__(self, parent, title_format, has_video, input_ac, current_settings, format_key):
        self.is_image_format = format_key in IMAGE_OUTPUT_FORMAT_KEYS
        dialog_size = (480, 400) if self.is_image_format else (560, 760)
        super().__init__(parent, title=_("Configure settings for: ") + title_format, size=dialog_size)
        self.current_settings = current_settings
        self.format_key = format_key
        self.has_video_controls = bool(has_video and format_key in VIDEO_CONTAINER_FORMAT_KEYS)

        self.video_preset_choice_keys = []
        self.video_crf_values = []
        self.audio_codec_keys = []

        self.main_sizer = wx.BoxSizer(wx.VERTICAL)

        if self.is_image_format:
            self._build_image_section()
        else:
            self._build_audio_section()
            if self.has_video_controls:
                self._build_video_section()
        self._build_buttons()

        self.SetSizer(self.main_sizer)
        self.Centre()

        if self.is_image_format:
            self._load_image_settings()
            self._set_image_accessibility_metadata()
        else:
            self._bind_events()
            self._load_from_settings()
            self._update_visibility()
            self._set_accessibility_metadata()
            wx.CallAfter(self._focus_primary_audio_control)

    def _build_audio_section(self):
        audio_box = wx.StaticBox(self, label=_("Audio Settings"))
        audio_box.SetWindowStyle(audio_box.GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        audio_sizer = wx.StaticBoxSizer(audio_box, wx.VERTICAL)

        row_mode = wx.BoxSizer(wx.HORIZONTAL)
        self.rb_convert = wx.RadioButton(self, label=_("Re-encode (Recommended)"), style=wx.RB_GROUP)
        self.rb_copy = wx.RadioButton(self, label=_("Copy Stream (Advanced)"))
        row_mode.Add(self.rb_convert, 0, wx.RIGHT, 15)
        row_mode.Add(self.rb_copy, 0)
        audio_sizer.Add(row_mode, 0, wx.ALL, 5)

        self.lbl_copy_warn = wx.StaticText(
            self,
            label=_(
                "Keep original quality and speed up conversion.\n"
                "Warning: The output format must support the source codec."
            ),
        )
        self.lbl_copy_warn.SetForegroundColour(wx.Colour(100, 100, 100))
        audio_sizer.Add(self.lbl_copy_warn, 0, wx.ALL | wx.EXPAND, 5)

        self.panel_audio_opts = wx.Panel(self)
        self.audio_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=10, hgap=10)
        self.audio_grid.AddGrowableCol(1, 1)

        if self.format_key in VIDEO_CONTAINER_FORMAT_KEYS:
            self.lbl_audio_codec = wx.StaticText(self.panel_audio_opts, label=_("Audio Codec:"))
            self.combo_audio_codec = wx.Choice(self.panel_audio_opts, choices=[])
            self.audio_grid.Add(self.lbl_audio_codec, 0, wx.ALIGN_CENTER_VERTICAL)
            self.audio_grid.Add(self.combo_audio_codec, 0, wx.EXPAND)
        else:
            self.lbl_audio_codec = None
            self.combo_audio_codec = None

        self.sr_display_choices = [_("Original"), "44.1 kHz", "48 kHz", "96 kHz", "22.05 kHz"]
        self.lbl_sr = wx.StaticText(self.panel_audio_opts, label=_("Sample Rate:"))
        self.combo_sr = wx.Choice(self.panel_audio_opts, choices=self.sr_display_choices)
        self.audio_grid.Add(self.lbl_sr, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_sr, 0, wx.EXPAND)

        self.ch_display_choices = [_("Stereo (Downmix)"), _("Mono"), _("Original Channels")]
        self.lbl_ch = wx.StaticText(self.panel_audio_opts, label=_("Channels:"))
        self.combo_ch = wx.Choice(self.panel_audio_opts, choices=self.ch_display_choices)
        self.audio_grid.Add(self.lbl_ch, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_ch, 0, wx.EXPAND)

        self.lbl_rate_mode = wx.StaticText(self.panel_audio_opts, label=_("Rate Mode:"))
        # Les choix (et leurs valeurs) dépendent du codec : MP3 expose en plus le mode ABR
        # (débit moyen ciblé), pas l'AAC. Peuplé via _populate_rate_mode_combo().
        self._rate_mode_values = ['cbr', 'vbr']
        self.combo_rate_mode = wx.Choice(self.panel_audio_opts, choices=[])
        self.audio_grid.Add(self.lbl_rate_mode, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_rate_mode, 0, wx.EXPAND)

        self.lbl_bitrate = wx.StaticText(self.panel_audio_opts, label=_("Bitrate:"))
        self.bitrate_display_choices = ['320k', '256k', '192k', '160k', '128k', '96k', '64k']
        self.combo_bitrate = wx.Choice(self.panel_audio_opts, choices=self.bitrate_display_choices)
        self.audio_grid.Add(self.lbl_bitrate, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_bitrate, 0, wx.EXPAND)

        self.lbl_quality = wx.StaticText(self.panel_audio_opts, label=_("Quality (VBR):"))
        self.combo_quality = wx.Choice(self.panel_audio_opts, choices=[])
        self.audio_grid.Add(self.lbl_quality, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_quality, 0, wx.EXPAND)

        self.lbl_depth = wx.StaticText(self.panel_audio_opts, label=_("Bit Depth:"))
        self.combo_depth = wx.Choice(
            self.panel_audio_opts,
            choices=[_("Original"), _("16-bit (CD Quality)"), _("24-bit (Studio Quality)"), _("32-bit Float (Pro)")],
        )
        self.audio_grid.Add(self.lbl_depth, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_depth, 0, wx.EXPAND)

        self.lbl_comp = wx.StaticText(self.panel_audio_opts, label=_("Compression Level:"))
        comp_choices = []
        for value in range(13):
            label = str(value)
            if value == 0:
                label += " (" + _("Fast") + ")"
            if value == 5:
                label += " (" + _("Standard") + ")"
            if value == 8:
                label += " (" + _("Max") + ")"
            if value == 12:
                label += " (" + _("Ultra Slow") + ")"
            comp_choices.append(label)
        self.combo_comp = wx.Choice(self.panel_audio_opts, choices=comp_choices)
        self.audio_grid.Add(self.lbl_comp, 0, wx.ALIGN_CENTER_VERTICAL)
        self.audio_grid.Add(self.combo_comp, 0, wx.EXPAND)

        self.chk_normalize_streaming = wx.CheckBox(
            self.panel_audio_opts,
            label=_("Normalize for streaming (-16 LUFS)"),
        )

        audio_opts_sizer = wx.BoxSizer(wx.VERTICAL)
        audio_opts_sizer.Add(self.audio_grid, 0, wx.EXPAND)
        audio_opts_sizer.Add(self.chk_normalize_streaming, 0, wx.TOP, 12)
        self.panel_audio_opts.SetSizer(audio_opts_sizer)
        audio_sizer.Add(self.panel_audio_opts, 1, wx.EXPAND | wx.ALL, 10)
        self.main_sizer.Add(audio_sizer, 0, wx.EXPAND | wx.ALL, 5)

    def _build_video_section(self):
        video_box = wx.StaticBox(self, label=_("Video Settings"))
        video_box.SetWindowStyle(video_box.GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        video_sizer = wx.StaticBoxSizer(video_box, wx.VERTICAL)

        row_vmode = wx.BoxSizer(wx.HORIZONTAL)
        self.rb_v_convert = wx.RadioButton(self, label=_("Re-encode (Recommended)"), style=wx.RB_GROUP)
        self.rb_v_copy = wx.RadioButton(self, label=_("Copy Stream (Advanced)"))
        row_vmode.Add(self.rb_v_convert, 0, wx.RIGHT, 15)
        row_vmode.Add(self.rb_v_copy, 0)
        video_sizer.Add(row_vmode, 0, wx.ALL, 5)

        self.panel_video_opts = wx.Panel(self)
        self.video_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=10, hgap=10)
        self.video_grid.AddGrowableCol(1, 1)

        self.lbl_video_preset = wx.StaticText(self.panel_video_opts, label=_("Video Preset:"))
        self.combo_video_preset = wx.Choice(self.panel_video_opts, choices=[])
        self.video_grid.Add(self.lbl_video_preset, 0, wx.ALIGN_CENTER_VERTICAL)
        self.video_grid.Add(self.combo_video_preset, 0, wx.EXPAND)

        self.lbl_crf = wx.StaticText(self.panel_video_opts, label=_("Quality (CRF):"))
        self.combo_crf = wx.Choice(self.panel_video_opts, choices=[])
        self.video_grid.Add(self.lbl_crf, 0, wx.ALIGN_CENTER_VERTICAL)
        self.video_grid.Add(self.combo_crf, 0, wx.EXPAND)

        self.lbl_video_encoder_preset = wx.StaticText(self.panel_video_opts, label=_("Encoder Preset:"))
        self.combo_video_encoder_preset = wx.Choice(self.panel_video_opts, choices=[])
        self.video_grid.Add(self.lbl_video_encoder_preset, 0, wx.ALIGN_CENTER_VERTICAL)
        self.video_grid.Add(self.combo_video_encoder_preset, 0, wx.EXPAND)

        self.lbl_video_profile = wx.StaticText(self.panel_video_opts, label=_("H.264 Profile:"))
        self.combo_video_profile = wx.Choice(self.panel_video_opts, choices=[])
        self.video_grid.Add(self.lbl_video_profile, 0, wx.ALIGN_CENTER_VERTICAL)
        self.video_grid.Add(self.combo_video_profile, 0, wx.EXPAND)

        self.lbl_video_pixel_format = wx.StaticText(self.panel_video_opts, label=_("Pixel Format:"))
        self.combo_video_pixel_format = wx.Choice(self.panel_video_opts, choices=[])
        self.video_grid.Add(self.lbl_video_pixel_format, 0, wx.ALIGN_CENTER_VERTICAL)
        self.video_grid.Add(self.combo_video_pixel_format, 0, wx.EXPAND)

        self.panel_video_opts.SetSizer(self.video_grid)
        video_sizer.Add(self.panel_video_opts, 1, wx.EXPAND | wx.ALL, 10)
        self.main_sizer.Add(video_sizer, 0, wx.EXPAND | wx.ALL, 5)

    def _build_buttons(self):
        btn_sizer = wx.StdDialogButtonSizer()
        btn_ok = wx.Button(self, wx.ID_OK, label=_("OK"))
        btn_cancel = wx.Button(self, wx.ID_CANCEL, label=_("Cancel"))
        btn_sizer.AddButton(btn_ok)
        btn_sizer.AddButton(btn_cancel)
        btn_sizer.Realize()
        self.main_sizer.Add(btn_sizer, 0, wx.ALIGN_RIGHT | wx.ALL, 10)
        # Entrée valide, Échap annule (sans tabuler jusqu'aux boutons).
        self.SetAffirmativeId(wx.ID_OK)
        self.SetEscapeId(wx.ID_CANCEL)

    def _bind_events(self):
        self.rb_convert.Bind(wx.EVT_RADIOBUTTON, self.on_mode_change)
        self.rb_copy.Bind(wx.EVT_RADIOBUTTON, self.on_mode_change)

        if self.combo_audio_codec:
            self.combo_audio_codec.Bind(wx.EVT_CHOICE, self.on_audio_codec_change)
        self.combo_sr.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.combo_ch.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.combo_rate_mode.Bind(wx.EVT_CHOICE, self.on_rate_mode_change)
        self.combo_bitrate.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.combo_quality.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.combo_depth.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.combo_comp.Bind(wx.EVT_CHOICE, self.on_audio_option_change)
        self.chk_normalize_streaming.Bind(wx.EVT_CHECKBOX, self.on_audio_option_change)

        if self.has_video_controls:
            self.rb_v_convert.Bind(wx.EVT_RADIOBUTTON, self.on_vmode_change)
            self.rb_v_copy.Bind(wx.EVT_RADIOBUTTON, self.on_vmode_change)
            self.combo_video_preset.Bind(wx.EVT_CHOICE, self.on_video_preset_change)
            self.combo_crf.Bind(wx.EVT_CHOICE, self.on_video_advanced_option_change)
            self.combo_video_encoder_preset.Bind(wx.EVT_CHOICE, self.on_video_advanced_option_change)
            self.combo_video_profile.Bind(wx.EVT_CHOICE, self.on_video_advanced_option_change)
            self.combo_video_pixel_format.Bind(wx.EVT_CHOICE, self.on_video_pixel_format_change)

    def _load_from_settings(self):
        settings = self.current_settings

        if settings.get("audio_mode") == "copy":
            self.rb_copy.SetValue(True)
        else:
            self.rb_convert.SetValue(True)

        self._populate_audio_codec_combo(settings.get("audio_codec"))

        sr_map = {'original': 0, '44100': 1, '48000': 2, '96000': 3, '22050': 4}
        self.combo_sr.SetSelection(sr_map.get(str(settings.get('audio_sample_rate', 'original')), 0))

        ch_map = {'2': 0, '1': 1, 'original': 2}
        self.combo_ch.SetSelection(ch_map.get(str(settings.get('audio_channels', '2')), 0))

        self._populate_rate_mode_combo(self._get_active_audio_codec_key())
        self._set_rate_mode_selection(settings.get('rate_mode', 'cbr'))

        br_map = {'320k': 0, '256k': 1, '192k': 2, '160k': 3, '128k': 4, '96k': 5, '64k': 6}
        self.combo_bitrate.SetSelection(br_map.get(settings.get('audio_bitrate', '192k'), 2))

        self._populate_quality_combo(self._get_active_audio_codec_key())
        self._set_quality_selection(settings)

        d_map = {'original': 0, '16': 1, '24': 2, '32': 3}
        self.combo_depth.SetSelection(d_map.get(str(settings.get('audio_bit_depth', 'original')), 0))

        compression = int(settings.get('flac_compression', 5))
        self.combo_comp.SetSelection(max(0, min(compression, 12)))
        self.chk_normalize_streaming.SetValue(bool(settings.get('audio_normalize_streaming', False)))

        if self.has_video_controls:
            if settings.get("video_mode") == "copy":
                self.rb_v_copy.SetValue(True)
            else:
                self.rb_v_convert.SetValue(True)

            self._populate_crf_combo(settings.get('video_crf', DEFAULT_FORMAT_SETTINGS[self.format_key]['video_crf']))
            self._populate_video_encoder_preset_combo(settings.get("video_encoder_preset"))
            self._populate_video_profile_combo(settings.get("video_profile"))
            self._populate_video_pixel_format_combo(settings.get("video_pixel_format"))
            self._sync_video_preset_selection_from_values(requested_key=settings.get("video_preset_profile"))

    def _populate_audio_codec_combo(self, selected_codec):
        if not self.combo_audio_codec:
            return

        self.audio_codec_keys = list(get_container_audio_codec_options(self.format_key))
        self.combo_audio_codec.Set([get_audio_codec_label(codec_key) for codec_key in self.audio_codec_keys])

        target_codec = str(selected_codec or DEFAULT_FORMAT_SETTINGS[self.format_key]["audio_codec"]).lower()
        if target_codec not in self.audio_codec_keys:
            target_codec = DEFAULT_FORMAT_SETTINGS[self.format_key]["audio_codec"]
        self.combo_audio_codec.SetSelection(self.audio_codec_keys.index(target_codec))

    def _populate_quality_combo(self, codec_key):
        self.combo_quality.Clear()
        choices = []
        if codec_key == 'mp3':
            for value in range(10):
                label = f"V{value}"
                if value == 0:
                    label += " (" + _("Best Quality") + ")"
                elif value == 2:
                    label += " (" + _("High Quality") + ")"
                elif value == 4:
                    label += " (" + _("Medium") + ")"
                elif value == 9:
                    label += " (" + _("Smallest Size") + ")"
                choices.append(label)
        elif codec_key == 'aac':
            for value in range(1, 6):
                label = f"Q{value}"
                if value == 1:
                    label += " (" + _("Low") + ")"
                elif value == 3:
                    label += " (" + _("Standard") + ")"
                elif value == 5:
                    label += " (" + _("High") + ")"
                choices.append(label)
        elif codec_key == 'ogg':
            for value in range(11):
                label = f"Q{value}"
                if value == 6:
                    label += " (" + _("Audiophile") + ")"
                choices.append(label)
        self.combo_quality.Set(choices)

    def _populate_rate_mode_combo(self, codec_key):
        if codec_key == 'mp3':
            labels = [_("Constant Bitrate (CBR)"), _("Average Bitrate (ABR)"), _("Variable Bitrate (VBR)")]
            self._rate_mode_values = ['cbr', 'abr', 'vbr']
        else:
            labels = [_("Constant Bitrate (CBR)"), _("Variable Bitrate (VBR)")]
            self._rate_mode_values = ['cbr', 'vbr']
        self.combo_rate_mode.Set(labels)

    def _current_rate_mode(self):
        idx = self.combo_rate_mode.GetSelection()
        if idx == wx.NOT_FOUND or idx >= len(self._rate_mode_values):
            return 'cbr'
        return self._rate_mode_values[idx]

    def _set_rate_mode_selection(self, mode):
        if mode in self._rate_mode_values:
            self.combo_rate_mode.SetSelection(self._rate_mode_values.index(mode))
        else:
            self.combo_rate_mode.SetSelection(0)

    def _set_quality_selection(self, settings):
        codec_key = self._get_active_audio_codec_key()
        quality = int(settings.get("audio_qscale", 0))
        if codec_key == "mp3":
            self.combo_quality.SetSelection(max(0, min(quality, 9)))
        elif codec_key == "aac":
            self.combo_quality.SetSelection(max(0, min(quality - 1, 4)))
        elif codec_key == "ogg":
            self.combo_quality.SetSelection(max(0, min(quality, 10)))
        else:
            self.combo_quality.SetSelection(wx.NOT_FOUND)

    def _populate_video_encoder_preset_combo(self, selected_key):
        self.video_encoder_preset_keys = [key for key, _msgid in VIDEO_ENCODER_PRESET_OPTIONS]
        self.combo_video_encoder_preset.Set([_(msgid) for _key, msgid in VIDEO_ENCODER_PRESET_OPTIONS])

        target_key = str(selected_key or DEFAULT_FORMAT_SETTINGS[self.format_key]["video_encoder_preset"]).lower()
        if target_key not in VALID_VIDEO_ENCODER_PRESETS:
            target_key = DEFAULT_FORMAT_SETTINGS[self.format_key]["video_encoder_preset"]
        self.combo_video_encoder_preset.SetSelection(self.video_encoder_preset_keys.index(target_key))

    def _populate_video_profile_combo(self, selected_key):
        self.video_profile_keys = [key for key, _msgid in VIDEO_PROFILE_OPTIONS]
        self.combo_video_profile.Set([_(msgid) for _key, msgid in VIDEO_PROFILE_OPTIONS])

        target_key = str(selected_key or DEFAULT_FORMAT_SETTINGS[self.format_key]["video_profile"]).lower()
        if target_key not in VALID_VIDEO_PROFILES:
            target_key = DEFAULT_FORMAT_SETTINGS[self.format_key]["video_profile"]
        self.combo_video_profile.SetSelection(self.video_profile_keys.index(target_key))

    def _populate_video_pixel_format_combo(self, selected_key):
        self.video_pixel_format_keys = [key for key, _msgid in VIDEO_PIXEL_FORMAT_OPTIONS]
        self.combo_video_pixel_format.Set([_(msgid) for _key, msgid in VIDEO_PIXEL_FORMAT_OPTIONS])

        target_key = str(selected_key or DEFAULT_FORMAT_SETTINGS[self.format_key]["video_pixel_format"]).lower()
        if target_key not in VALID_VIDEO_PIXEL_FORMATS:
            target_key = DEFAULT_FORMAT_SETTINGS[self.format_key]["video_pixel_format"]
        self.combo_video_pixel_format.SetSelection(self.video_pixel_format_keys.index(target_key))

    def on_mode_change(self, event):
        self._update_visibility()
        if self.rb_convert.GetValue():
            self._focus_primary_audio_control()
            wx.CallAfter(self._focus_primary_audio_control)
        event.Skip()

    def on_vmode_change(self, event):
        self._update_visibility()
        event.Skip()

    def on_rate_mode_change(self, event):
        self._update_visibility(preserve_focus=self.combo_rate_mode)
        event.Skip()

    def on_audio_codec_change(self, event):
        codec_key = self._get_active_audio_codec_key()
        current_mode = self._current_rate_mode()
        self._populate_quality_combo(codec_key)
        self._set_quality_selection(self.current_settings)
        self._populate_rate_mode_combo(codec_key)
        self._set_rate_mode_selection(current_mode)
        self._update_visibility(preserve_focus=self.combo_audio_codec)
        event.Skip()

    def on_audio_option_change(self, event):
        self._update_dynamic_accessible_names()
        event.Skip()

    def on_video_preset_change(self, event):
        preset_key = self._get_selected_video_preset_key()
        if preset_key and preset_key != "custom":
            self._apply_video_preset_to_controls(preset_key)
            self._sync_video_preset_selection_from_values(requested_key=preset_key)
        self._update_dynamic_accessible_names()
        event.Skip()

    def on_video_advanced_option_change(self, event):
        self._sync_video_preset_selection_from_values(requested_key="custom")
        self._update_profile_availability()
        self._update_dynamic_accessible_names()
        event.Skip()

    def on_video_pixel_format_change(self, event):
        self._sync_video_preset_selection_from_values(requested_key="custom")
        self._update_profile_availability()
        self._update_dynamic_accessible_names()
        event.Skip()

    def _update_visibility(self, preserve_focus=None):
        is_audio_convert = self.rb_convert.GetValue()
        self.panel_audio_opts.Enable(is_audio_convert)
        self.lbl_copy_warn.Show(not is_audio_convert)

        for widget in (
            self.lbl_rate_mode,
            self.combo_rate_mode,
            self.lbl_bitrate,
            self.combo_bitrate,
            self.lbl_quality,
            self.combo_quality,
            self.lbl_depth,
            self.combo_depth,
            self.lbl_comp,
            self.combo_comp,
        ):
            widget.Hide()

        if is_audio_convert:
            codec_key = self._get_active_audio_codec_key()
            if codec_key in ("mp3", "aac"):
                self.lbl_rate_mode.Show()
                self.combo_rate_mode.Show()
                if self._current_rate_mode() == 'vbr':
                    self.lbl_quality.SetLabel(_("Quality (VBR):"))
                    self.lbl_quality.Show()
                    self.combo_quality.Show()
                else:
                    # CBR comme ABR utilisent le sélecteur de débit (moyenne ciblée en ABR).
                    self.lbl_bitrate.Show()
                    self.combo_bitrate.Show()
            elif codec_key == "ogg":
                self.lbl_quality.SetLabel(_("Quality (OGG):"))
                self.lbl_quality.Show()
                self.combo_quality.Show()
            elif codec_key in ("wma", "opus"):
                self.lbl_bitrate.Show()
                self.combo_bitrate.Show()
            elif codec_key in ("wav", "flac", "alac"):
                self.lbl_depth.Show()
                self.combo_depth.Show()
                if codec_key == "flac":
                    self.lbl_comp.Show()
                    self.combo_comp.Show()

        self.chk_normalize_streaming.Enable(is_audio_convert)

        if self.has_video_controls:
            self.panel_video_opts.Enable(self.rb_v_convert.GetValue())
            self._update_profile_availability()

        self._update_dynamic_accessible_names()
        self.panel_audio_opts.Layout()
        if self.has_video_controls:
            self.panel_video_opts.Layout()
        self.main_sizer.Layout()

        if preserve_focus and preserve_focus.IsShown() and preserve_focus.IsEnabled():
            current_focus = wx.Window.FindFocus()
            if current_focus is not preserve_focus:
                wx.CallAfter(preserve_focus.SetFocus)

    def _update_profile_availability(self):
        if not self.has_video_controls:
            return

        profile_enabled = self.rb_v_convert.GetValue() and self._get_selected_video_pixel_format_key() != "yuv444p"
        self.lbl_video_profile.Enable(profile_enabled)
        self.combo_video_profile.Enable(profile_enabled)

    def _set_accessibility_metadata(self):
        self.SetName(_("Format settings dialog"))
        self.panel_audio_opts.SetName(_("Audio settings panel"))
        self.panel_audio_opts.SetToolTip(_("Use Tab to navigate audio options."))

        self.rb_convert.SetName(_("Audio mode re-encode"))
        self.rb_copy.SetName(_("Audio mode copy stream"))
        self.rb_convert.SetToolTip(_("Re-encode audio with detailed settings."))
        self.rb_copy.SetToolTip(_("Copy source audio without re-encoding."))

        if self.combo_audio_codec:
            self.combo_audio_codec.SetName(_("Audio Codec"))
            self.combo_audio_codec.SetToolTip(_("Choose the audio codec used for the container output."))

        self.combo_sr.SetName(_("Sample Rate"))
        self.combo_ch.SetName(_("Channels"))
        self.combo_rate_mode.SetName(_("Rate Mode"))
        self.combo_bitrate.SetName(_("Bitrate"))
        self.combo_quality.SetName(_("Quality"))
        self.combo_depth.SetName(_("Bit Depth"))
        self.combo_comp.SetName(_("Compression"))
        self.chk_normalize_streaming.SetName(_("Normalize for streaming"))

        self.combo_sr.SetToolTip(_("Target sample rate."))
        self.combo_ch.SetToolTip(_("Target channel layout."))
        self.combo_rate_mode.SetToolTip(_("Choose CBR, ABR (MP3 only) or VBR mode."))
        self.combo_bitrate.SetToolTip(_("Bitrate used in CBR mode (target average in ABR)."))
        self.combo_quality.SetToolTip(_("Quality scale used in VBR mode."))
        self.combo_depth.SetToolTip(_("Bit depth for lossless formats."))
        self.combo_comp.SetToolTip(_("Compression level for FLAC."))
        self.chk_normalize_streaming.SetToolTip(_("Apply streaming loudness normalization at -16 LUFS."))

        self.lbl_copy_warn.SetName(_("Copy mode warning"))

        if self.has_video_controls:
            self.panel_video_opts.SetName(_("Video settings panel"))
            self.rb_v_convert.SetName(_("Video mode re-encode"))
            self.rb_v_copy.SetName(_("Video mode copy stream"))
            self.combo_video_preset.SetName(_("Video Preset"))
            self.combo_crf.SetName(_("Video quality CRF"))
            self.combo_video_encoder_preset.SetName(_("Encoder Preset"))
            self.combo_video_profile.SetName(_("H.264 Profile"))
            self.combo_video_pixel_format.SetName(_("Pixel Format"))
            self.combo_crf.SetToolTip(_("Lower CRF means better quality and bigger file."))
            self.combo_video_encoder_preset.SetToolTip(_("Choose the x264 speed preset used for encoding."))
            self.combo_video_profile.SetToolTip(_("Choose the H.264 profile used for compatible outputs."))
            self.combo_video_pixel_format.SetToolTip(_("Choose the output pixel format."))

        self._update_dynamic_accessible_names()

    def _update_dynamic_accessible_names(self):
        if self.combo_audio_codec:
            self.combo_audio_codec.SetName(_("Audio Codec"))
        self.combo_sr.SetName(_("Sample Rate"))
        self.combo_ch.SetName(_("Channels"))
        self.combo_rate_mode.SetName(_("Rate Mode"))
        self.combo_bitrate.SetName(_("Bitrate"))
        self.combo_quality.SetName(_("Quality"))
        self.combo_depth.SetName(_("Bit Depth"))
        self.combo_comp.SetName(_("Compression"))
        self.chk_normalize_streaming.SetName(_("Normalize for streaming"))
        if self.has_video_controls:
            self.combo_video_preset.SetName(_("Video Preset"))
            self.combo_crf.SetName(_("Video quality CRF"))
            self.combo_video_encoder_preset.SetName(_("Encoder Preset"))
            self.combo_video_profile.SetName(_("H.264 Profile"))
            self.combo_video_pixel_format.SetName(_("Pixel Format"))

    def _focus_primary_audio_control(self):
        ordered_controls = []
        if self.combo_audio_codec:
            ordered_controls.append(self.combo_audio_codec)
        ordered_controls.extend(
            [
                self.combo_sr,
                self.combo_ch,
                self.combo_rate_mode,
                self.combo_bitrate,
                self.combo_quality,
                self.combo_depth,
                self.combo_comp,
                self.chk_normalize_streaming,
            ]
        )
        for ctrl in ordered_controls:
            if ctrl.IsShown() and ctrl.IsEnabled():
                ctrl.SetFocus()
                return

    def _coerce_video_crf_value(self, value):
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return DEFAULT_FORMAT_SETTINGS[self.format_key]['video_crf']

        if 0 <= parsed <= 51:
            return parsed
        return DEFAULT_FORMAT_SETTINGS[self.format_key]['video_crf']

    def _build_crf_choice_label(self, value, label_msgid=None, custom=False):
        label = _("Custom") if custom else _(label_msgid or "Balanced - Recommended")
        return _("CRF {value} ({label})").format(value=value, label=label)

    def _populate_crf_combo(self, current_value):
        value = self._coerce_video_crf_value(current_value)
        preset_values = {preset_value for preset_value, _label_msgid in VIDEO_CRF_PRESET_OPTIONS}
        custom_value = value if value not in preset_values else None

        self.video_crf_values = []
        choices = []
        custom_inserted = False

        for preset_value, label_msgid in VIDEO_CRF_PRESET_OPTIONS:
            if custom_value is not None and not custom_inserted and custom_value < preset_value:
                self.video_crf_values.append(custom_value)
                choices.append(self._build_crf_choice_label(custom_value, custom=True))
                custom_inserted = True

            self.video_crf_values.append(preset_value)
            choices.append(self._build_crf_choice_label(preset_value, label_msgid=label_msgid))

        if custom_value is not None and not custom_inserted:
            self.video_crf_values.append(custom_value)
            choices.append(self._build_crf_choice_label(custom_value, custom=True))

        self.combo_crf.Set(choices)
        selected_value = (
            value if value in self.video_crf_values
            else DEFAULT_FORMAT_SETTINGS[self.format_key]["video_crf"]
        )
        self.combo_crf.SetSelection(self.video_crf_values.index(selected_value))

    def _refresh_video_preset_choices(self, selected_key):
        keys = [key for key, _msgid in VIDEO_PRESET_OPTIONS]
        if selected_key == "custom":
            keys.append("custom")

        choice_map = dict(VIDEO_PRESET_OPTIONS)
        choices = []
        for key in keys:
            choices.append(_("Custom") if key == "custom" else _(choice_map[key]))

        self.video_preset_choice_keys = keys
        self.combo_video_preset.Set(choices)
        self.combo_video_preset.SetSelection(self.video_preset_choice_keys.index(selected_key))

    def _get_selected_video_preset_key(self):
        selection = self.combo_video_preset.GetSelection()
        if selection == wx.NOT_FOUND or not self.video_preset_choice_keys:
            return None
        return self.video_preset_choice_keys[selection]

    def _get_selected_audio_codec_key(self):
        if not self.combo_audio_codec:
            return None
        selection = self.combo_audio_codec.GetSelection()
        if selection == wx.NOT_FOUND or not self.audio_codec_keys:
            return DEFAULT_FORMAT_SETTINGS[self.format_key]["audio_codec"]
        return self.audio_codec_keys[selection]

    def _get_active_audio_codec_key(self):
        if self.format_key in VIDEO_CONTAINER_FORMAT_KEYS:
            return self._get_selected_audio_codec_key()
        if self.format_key == 'm4b':
            return 'aac'  # M4B = conteneur AAC : mêmes contrôles que l'AAC
        return self.format_key

    def _get_selected_video_encoder_preset_key(self):
        selection = self.combo_video_encoder_preset.GetSelection()
        if selection == wx.NOT_FOUND:
            return DEFAULT_FORMAT_SETTINGS[self.format_key]["video_encoder_preset"]
        return self.video_encoder_preset_keys[selection]

    def _get_selected_video_profile_key(self):
        selection = self.combo_video_profile.GetSelection()
        if selection == wx.NOT_FOUND:
            return DEFAULT_FORMAT_SETTINGS[self.format_key]["video_profile"]
        return self.video_profile_keys[selection]

    def _get_selected_video_pixel_format_key(self):
        selection = self.combo_video_pixel_format.GetSelection()
        if selection == wx.NOT_FOUND:
            return DEFAULT_FORMAT_SETTINGS[self.format_key]["video_pixel_format"]
        return self.video_pixel_format_keys[selection]

    def _get_current_video_advanced_settings(self):
        selected_crf_index = self.combo_crf.GetSelection()
        if selected_crf_index == wx.NOT_FOUND:
            video_crf = DEFAULT_FORMAT_SETTINGS[self.format_key]["video_crf"]
        else:
            video_crf = self.video_crf_values[selected_crf_index]

        return {
            "video_crf": video_crf,
            "video_encoder_preset": self._get_selected_video_encoder_preset_key(),
            "video_profile": self._get_selected_video_profile_key(),
            "video_pixel_format": self._get_selected_video_pixel_format_key(),
        }

    def _apply_video_preset_to_controls(self, preset_key):
        preset_settings = VIDEO_PRESET_PROFILE_SETTINGS.get(preset_key)
        if not preset_settings:
            return

        self._populate_crf_combo(preset_settings["video_crf"])
        self.combo_video_encoder_preset.SetSelection(
            self.video_encoder_preset_keys.index(preset_settings["video_encoder_preset"])
        )
        self.combo_video_profile.SetSelection(self.video_profile_keys.index(preset_settings["video_profile"]))
        self.combo_video_pixel_format.SetSelection(
            self.video_pixel_format_keys.index(preset_settings["video_pixel_format"])
        )

    def _sync_video_preset_selection_from_values(self, requested_key=None):
        matched_key = get_matching_video_preset_profile(self._get_current_video_advanced_settings())
        if matched_key:
            self._refresh_video_preset_choices(matched_key)
            return

        requested_key = str(requested_key or "").strip()
        if requested_key == "custom" or requested_key in VIDEO_PRESET_PROFILE_SETTINGS:
            self._refresh_video_preset_choices("custom")
            return

        self._refresh_video_preset_choices("custom")

    def _build_image_section(self):
        image_box = wx.StaticBox(self, label=_("Image Settings"))
        image_box.SetWindowStyle(image_box.GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        image_sizer = wx.StaticBoxSizer(image_box, wx.VERTICAL)

        self.panel_image_opts = wx.Panel(self)
        self.image_grid = wx.FlexGridSizer(rows=0, cols=2, vgap=10, hgap=10)
        self.image_grid.AddGrowableCol(1, 1)

        fmt = self.format_key

        self.spin_image_quality = None
        self.spin_image_compression = None
        self.chk_image_lossless = None
        self.combo_tiff_compression = None
        self.combo_image_resize = None

        if fmt in ('jpeg', 'webp'):
            self.lbl_image_quality = wx.StaticText(self.panel_image_opts, label=_("Quality (1-100):"))
            self.spin_image_quality = wx.SpinCtrl(self.panel_image_opts, min=1, max=100, initial=85)
            self.image_grid.Add(self.lbl_image_quality, 0, wx.ALIGN_CENTER_VERTICAL)
            self.image_grid.Add(self.spin_image_quality, 0, wx.EXPAND)

        if fmt == 'webp':
            self.chk_image_lossless = wx.CheckBox(self.panel_image_opts, label=_("Lossless"))
            self.image_grid.AddSpacer(0)
            self.image_grid.Add(self.chk_image_lossless, 0)

        if fmt == 'png':
            self.lbl_image_compression = wx.StaticText(self.panel_image_opts, label=_("Compression Level (0-9):"))
            self.spin_image_compression = wx.SpinCtrl(self.panel_image_opts, min=0, max=9, initial=6)
            self.image_grid.Add(self.lbl_image_compression, 0, wx.ALIGN_CENTER_VERTICAL)
            self.image_grid.Add(self.spin_image_compression, 0, wx.EXPAND)

        if fmt == 'tiff':
            self.lbl_tiff_compression = wx.StaticText(self.panel_image_opts, label=_("Compression:"))
            tiff_choices = ["LZW", "Deflate", "PackBits", _("None")]
            # 'raw' = jeton FFmpeg « non compressé » (affiché « None » côté UI).
            self.tiff_compression_keys = ["lzw", "deflate", "packbits", "raw"]
            self.combo_tiff_compression = wx.Choice(self.panel_image_opts, choices=tiff_choices)
            self.image_grid.Add(self.lbl_tiff_compression, 0, wx.ALIGN_CENTER_VERTICAL)
            self.image_grid.Add(self.combo_tiff_compression, 0, wx.EXPAND)

        self.lbl_image_resize = wx.StaticText(self.panel_image_opts, label=_("Resize:"))
        resize_labels = [label for _, label in IMAGE_RESIZE_OPTIONS]
        self.image_resize_keys = [key for key, _ in IMAGE_RESIZE_OPTIONS]
        self.combo_image_resize = wx.Choice(self.panel_image_opts, choices=resize_labels)
        self.image_grid.Add(self.lbl_image_resize, 0, wx.ALIGN_CENTER_VERTICAL)
        self.image_grid.Add(self.combo_image_resize, 0, wx.EXPAND)

        self.panel_image_opts.SetSizer(self.image_grid)
        image_sizer.Add(self.panel_image_opts, 1, wx.EXPAND | wx.ALL, 10)
        self.main_sizer.Add(image_sizer, 0, wx.EXPAND | wx.ALL, 5)

    def _load_image_settings(self):
        settings = self.current_settings
        fmt = self.format_key

        if self.spin_image_quality and fmt in ('jpeg', 'webp'):
            default_q = 85 if fmt == 'jpeg' else 80
            self.spin_image_quality.SetValue(int(settings.get('image_quality', default_q)))

        if self.chk_image_lossless and fmt == 'webp':
            self.chk_image_lossless.SetValue(bool(settings.get('image_lossless', False)))

        if self.spin_image_compression and fmt == 'png':
            self.spin_image_compression.SetValue(int(settings.get('image_compression', 6)))

        if self.combo_tiff_compression and fmt == 'tiff':
            comp = str(settings.get('image_compression', 'lzw')).lower()
            if comp in self.tiff_compression_keys:
                self.combo_tiff_compression.SetSelection(self.tiff_compression_keys.index(comp))
            else:
                self.combo_tiff_compression.SetSelection(0)

        if self.combo_image_resize:
            resize = settings.get('image_resize', 'original')
            if resize in self.image_resize_keys:
                self.combo_image_resize.SetSelection(self.image_resize_keys.index(resize))
            else:
                self.combo_image_resize.SetSelection(0)

    def _set_image_accessibility_metadata(self):
        self.SetName(_("Image format settings dialog"))
        self.panel_image_opts.SetName(_("Image settings panel"))
        self.panel_image_opts.SetToolTip(_("Use Tab to navigate image options."))

        if self.spin_image_quality:
            self.spin_image_quality.SetName(_("Image quality"))
            self.spin_image_quality.SetToolTip(_("Quality from 1 (lowest) to 100 (highest)."))
        if self.chk_image_lossless:
            self.chk_image_lossless.SetName(_("Lossless mode"))
            self.chk_image_lossless.SetToolTip(_("Enable lossless compression for WebP."))
        if self.spin_image_compression:
            self.spin_image_compression.SetName(_("Compression level"))
            self.spin_image_compression.SetToolTip(_("PNG compression from 0 (fast) to 9 (smallest)."))
        if self.combo_tiff_compression:
            self.combo_tiff_compression.SetName(_("TIFF compression"))
            self.combo_tiff_compression.SetToolTip(_("Choose the compression algorithm for TIFF."))
        if self.combo_image_resize:
            self.combo_image_resize.SetName(_("Resize"))
            self.combo_image_resize.SetToolTip(_("Choose a target resolution. Aspect ratio is preserved."))

    def _get_image_settings(self):
        settings = dict(self.current_settings)
        fmt = self.format_key

        if self.spin_image_quality and fmt in ('jpeg', 'webp'):
            settings['image_quality'] = self.spin_image_quality.GetValue()

        if self.chk_image_lossless and fmt == 'webp':
            settings['image_lossless'] = self.chk_image_lossless.GetValue()

        if self.spin_image_compression and fmt == 'png':
            settings['image_compression'] = self.spin_image_compression.GetValue()

        if self.combo_tiff_compression and fmt == 'tiff':
            sel = self.combo_tiff_compression.GetSelection()
            if sel != wx.NOT_FOUND:
                settings['image_compression'] = self.tiff_compression_keys[sel]

        if self.combo_image_resize:
            sel = self.combo_image_resize.GetSelection()
            if sel != wx.NOT_FOUND:
                settings['image_resize'] = self.image_resize_keys[sel]

        settings['summary'] = build_image_format_summary(fmt, settings)
        return settings

    def get_settings(self):
        if self.is_image_format:
            return self._get_image_settings()

        settings = dict(self.current_settings)
        settings['audio_mode'] = 'copy' if self.rb_copy.GetValue() else 'convert'

        sr_vals = ['original', '44100', '48000', '96000', '22050']
        settings['audio_sample_rate'] = sr_vals[self.combo_sr.GetSelection()]

        ch_vals = ['2', '1', 'original']
        settings['audio_channels'] = ch_vals[self.combo_ch.GetSelection()]

        settings['rate_mode'] = self._current_rate_mode()

        br_vals = ['320k', '256k', '192k', '160k', '128k', '96k', '64k']
        settings['audio_bitrate'] = br_vals[self.combo_bitrate.GetSelection()]

        qual_idx = self.combo_quality.GetSelection()
        if qual_idx == wx.NOT_FOUND:
            qual_idx = int(
                self.current_settings.get(
                    'audio_qscale',
                    DEFAULT_FORMAT_SETTINGS.get(self._get_active_audio_codec_key(), DEFAULT_FORMAT_SETTINGS['mp3']).get(
                        'audio_qscale',
                        0,
                    ),
                )
            )

        active_codec = self._get_active_audio_codec_key()
        settings['audio_qscale'] = qual_idx + 1 if active_codec == 'aac' else qual_idx

        d_vals = ['original', '16', '24', '32']
        settings['audio_bit_depth'] = d_vals[self.combo_depth.GetSelection()]
        settings['flac_compression'] = self.combo_comp.GetSelection()
        settings['audio_normalize_streaming'] = self.chk_normalize_streaming.GetValue()

        if self.combo_audio_codec:
            settings["audio_codec"] = self._get_selected_audio_codec_key()

        if self.has_video_controls:
            settings['video_mode'] = 'copy' if self.rb_v_copy.GetValue() else 'convert'
            video_settings = self._get_current_video_advanced_settings()
            settings.update(video_settings)
            matched_preset = get_matching_video_preset_profile(video_settings)
            settings["video_preset_profile"] = matched_preset or "custom"

        settings['summary'] = build_format_summary(self.format_key, settings)
        return settings
