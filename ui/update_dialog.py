import logging
import re
import threading

import wx

from core.app_info import APP_NAME, APP_VERSION
from core.updater import (
    UpdateDownloadError,
    download_release_installer,
    format_release_date,
    open_release_page,
)


class UpdateDialog(wx.Dialog):
    def __init__(self, parent, release_info):
        super().__init__(parent, title=_("Update Available"), size=(760, 620))
        self.SetName(_("Update Available"))

        self.parent_window = parent
        self.release_info = release_info
        self._download_in_progress = False

        self._init_ui()
        self.Centre()

    def _init_ui(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=_("A new version of {app_name} is available.").format(app_name=APP_NAME),
        )
        intro.Wrap(680)
        root.Add(intro, 0, wx.EXPAND | wx.ALL, 12)

        info_grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        info_grid.AddGrowableCol(1, 1)

        current_version = wx.StaticText(panel, label=_("Current version:"))
        current_value = wx.StaticText(panel, label=APP_VERSION)
        available_version = wx.StaticText(panel, label=_("Available version:"))
        available_value = wx.StaticText(panel, label=self.release_info.version)
        published_date = wx.StaticText(panel, label=_("Published:"))
        published_value = wx.StaticText(panel, label=format_release_date(self.release_info.published_at))

        info_grid.Add(current_version, 0, wx.ALIGN_CENTER_VERTICAL)
        info_grid.Add(current_value, 0, wx.ALIGN_CENTER_VERTICAL)
        info_grid.Add(available_version, 0, wx.ALIGN_CENTER_VERTICAL)
        info_grid.Add(available_value, 0, wx.ALIGN_CENTER_VERTICAL)
        info_grid.Add(published_date, 0, wx.ALIGN_CENTER_VERTICAL)
        info_grid.Add(published_value, 0, wx.ALIGN_CENTER_VERTICAL)
        root.Add(info_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        notes_box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Release Notes"))
        notes_box.GetStaticBox().SetWindowStyle(notes_box.GetStaticBox().GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        self.txt_release_notes = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_RICH2 | wx.VSCROLL | wx.HSCROLL,
        )
        self.txt_release_notes.SetMinSize((-1, 320))
        self.txt_release_notes.SetName(_("Release Notes"))
        self.txt_release_notes.SetToolTip(_("Release notes for the selected update."))
        notes_box.Add(self.txt_release_notes, 1, wx.EXPAND | wx.ALL, 8)
        root.Add(notes_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.lbl_feedback = wx.StaticText(panel, label="")
        self.lbl_feedback.Wrap(680)
        self.lbl_feedback.Hide()
        root.Add(self.lbl_feedback, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.gauge_download = wx.Gauge(panel, range=100)
        self.gauge_download.Hide()
        root.Add(self.gauge_download, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.lbl_download_status = wx.StaticText(panel, label="")
        self.lbl_download_status.Hide()
        root.Add(self.lbl_download_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_download_install = wx.Button(panel, label=_("Download and Install"))
        self.btn_download_install.SetName(_("Download and Install"))
        self.btn_download_install.SetToolTip(_("Download the installer and start the update."))
        self.btn_download_install.SetDefault()

        self.btn_release_page = wx.Button(panel, label=_("Open Release Page"))
        self.btn_release_page.SetName(_("Open Release Page"))
        self.btn_release_page.SetToolTip(_("Open the GitHub release page in your browser."))

        self.btn_close = wx.Button(panel, wx.ID_CLOSE, label=_("Close"))
        self.btn_close.SetName(_("Close"))

        actions.Add(self.btn_download_install, 0, wx.RIGHT, 8)
        actions.Add(self.btn_release_page, 0, wx.RIGHT, 8)
        actions.AddStretchSpacer()
        actions.Add(self.btn_close, 0)
        root.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(root)
        self.SetEscapeId(self.btn_close.GetId())
        self.SetAffirmativeId(self.btn_download_install.GetId())

        self.Bind(wx.EVT_BUTTON, self.on_download_install, self.btn_download_install)
        self.Bind(wx.EVT_BUTTON, self.on_open_release_page, self.btn_release_page)
        self.Bind(wx.EVT_BUTTON, self.on_close_button, self.btn_close)
        self.Bind(wx.EVT_CLOSE, self.on_close_window)
        self.txt_release_notes.Bind(wx.EVT_KEY_DOWN, self.on_release_notes_key_down)
        self._render_release_notes()
        wx.CallAfter(self._set_initial_focus)

    def _render_release_notes(self):
        try:
            self.txt_release_notes.SetValue(self._to_plain_release_notes(self.release_info.body))
            self.txt_release_notes.SetInsertionPoint(0)
            self.txt_release_notes.ShowPosition(0)
        except Exception:  # noqa: BLE001
            logging.exception("Unable to render release notes as plain text.")

    def _set_initial_focus(self):
        self.txt_release_notes.SetFocus()

    def _to_plain_release_notes(self, body):
        lines = str(body or "").replace("\r\n", "\n").split("\n")
        normalized_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                normalized_lines.append(self._normalize_inline_text(stripped[3:]))
                continue
            if stripped.startswith("# "):
                normalized_lines.append(self._normalize_inline_text(stripped[2:]))
                continue
            if stripped.startswith("- "):
                normalized_lines.append(f"• {self._normalize_inline_text(stripped[2:])}")
                continue
            if stripped.startswith("* "):
                normalized_lines.append(f"• {self._normalize_inline_text(stripped[2:])}")
                continue
            normalized_lines.append(self._normalize_inline_text(line))
        return "\n".join(normalized_lines).strip()

    def _normalize_inline_text(self, text):
        normalized = str(text or "")
        normalized = re.sub(r"\*\*(.*?)\*\*", r"\1", normalized)
        normalized = re.sub(r"`(.*?)`", r"\1", normalized)
        normalized = re.sub(r"\[(.*?)\]\((.*?)\)", r"\1 (\2)", normalized)
        return normalized

    def _set_feedback(self, message, is_error=False):
        self.lbl_feedback.SetLabel(message)
        self.lbl_feedback.SetForegroundColour(
            wx.Colour(180, 0, 0) if is_error else wx.Colour(0, 120, 0)
        )
        self.lbl_feedback.Show()
        self.Layout()
        wx.CallAfter(self.lbl_feedback.SetFocus)

    def _set_download_state(self, downloading):
        self._download_in_progress = downloading
        self.btn_download_install.Enable(not downloading)
        self.btn_release_page.Enable(not downloading)
        self.btn_close.Enable(not downloading)
        if downloading:
            self.gauge_download.SetValue(0)
            self.gauge_download.Show()
            self.lbl_download_status.SetLabel(_("Downloading update..."))
            self.lbl_download_status.Show()
        self.Layout()

    def _update_download_progress(self, downloaded, total):
        if not self._download_in_progress:
            return

        if total > 0:
            percent = min(100, int((downloaded / total) * 100))
            self.gauge_download.SetValue(percent)
            status = _("Downloading update... {percent}%").format(percent=percent)
        else:
            self.gauge_download.Pulse()
            status = _("Downloading update...")

        self.lbl_download_status.SetLabel(status)
        self.lbl_download_status.SetName(status)
        self.Layout()

    def _on_download_success(self, installer_path):
        self._set_download_state(False)
        self.gauge_download.SetValue(100)
        self._set_feedback(_("Update downloaded successfully."))

        message = _(
            "Version {version} has been downloaded.\n\nThe application will close and the installer will start.\nDo you want to continue?"  # noqa: E501
        ).format(version=self.release_info.version)
        confirm = wx.MessageDialog(
            self,
            message,
            _("Install Update"),
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION,
        )
        try:
            if confirm.ShowModal() == wx.ID_YES:
                if self.parent_window.begin_install_update(installer_path, self.release_info.version):
                    self.EndModal(wx.ID_OK)
                    return
        finally:
            confirm.Destroy()

        try:
            installer_path.unlink(missing_ok=True)
        except OSError:
            logging.exception("Unable to remove the downloaded installer after cancel.")
        self._set_feedback(_("Update installation cancelled."))

    def _on_download_failure(self, message):
        self._set_download_state(False)
        self.gauge_download.Hide()
        self.lbl_download_status.Hide()
        self._set_feedback(message, is_error=True)
        self.Layout()

    def _download_worker(self):
        try:
            installer_path = download_release_installer(
                self.release_info,
                progress_callback=lambda downloaded, total: wx.CallAfter(
                    self._update_download_progress,
                    downloaded,
                    total,
                ),
            )
        except UpdateDownloadError as exc:
            wx.CallAfter(self._on_download_failure, str(exc))
            return
        except Exception:  # noqa: BLE001
            logging.exception("Unexpected error while downloading update.")
            wx.CallAfter(self._on_download_failure, _("Unexpected error while downloading the update."))
            return

        wx.CallAfter(self._on_download_success, installer_path)

    def on_download_install(self, event):
        if self._download_in_progress:
            return

        self._set_feedback(_("Downloading the installer..."))
        self._set_download_state(True)

        worker = threading.Thread(target=self._download_worker, daemon=True)
        worker.start()

    def on_open_release_page(self, event):
        try:
            open_release_page(self.release_info.html_url)
        except Exception:  # noqa: BLE001
            logging.exception("Unable to open the release page.")
            self._set_feedback(_("Unable to open the release page."), is_error=True)

    def on_release_notes_key_down(self, event):
        if event.GetKeyCode() == wx.WXK_TAB:
            target = self.btn_close if event.ShiftDown() else self.btn_download_install
            target.SetFocus()
            return
        event.Skip()

    def on_close_button(self, event):
        self.EndModal(wx.ID_CLOSE)

    def on_close_window(self, event):
        if self._download_in_progress:
            self._set_feedback(_("Please wait for the update download to finish."), is_error=True)
            event.Veto()
            return
        event.Skip()
