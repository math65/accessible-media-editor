import logging
import threading

import wx

from core.app_info import SUPPORT_EMAIL
from core.support import (
    build_support_report,
    build_support_subject,
    build_support_technical_block,
    collect_support_context,
    get_support_issue_type_items,
    send_support_report,
    validate_support_email,
    validate_support_form,
)


class SupportContactDialog(wx.Dialog):
    def __init__(self, parent):
        super().__init__(parent, title=_("Contact Support"), size=(720, 560))
        self.SetName(_("Contact Support"))

        self.parent_window = parent
        self.contact_email = SUPPORT_EMAIL
        self.saved_user_email = str(parent._settings.get("support_user_email", "") or "")
        self.support_context = collect_support_context(parent)
        self.issue_type_items = list(get_support_issue_type_items())
        self._send_in_progress = False
        self._technical_details_visible = False

        self._init_ui()
        self._refresh_generated_content()
        self.Centre()

    def _init_ui(self):
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        intro = wx.StaticText(
            panel,
            label=_(
                "Describe your issue below. The report will be sent directly to support."
            ),
        )
        intro.Wrap(640)
        root.Add(intro, 0, wx.EXPAND | wx.ALL, 12)

        self.lbl_feedback = wx.StaticText(panel, label="")
        self.lbl_feedback.Wrap(640)
        self.lbl_feedback.Hide()
        root.Add(self.lbl_feedback, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        form_grid = wx.FlexGridSizer(cols=2, vgap=8, hgap=10)
        form_grid.AddGrowableCol(1, 1)

        lbl_email = wx.StaticText(panel, label=_("Your email (required)"))
        self.txt_email = wx.TextCtrl(panel)
        self.txt_email.SetName(_("Your email (required)"))
        self.txt_email.SetToolTip(_("Enter the email address that support should reply to."))

        lbl_issue = wx.StaticText(panel, label=_("Issue type"))
        self.choice_issue_type = wx.Choice(
            panel,
            choices=[_(msgid) for issue_code, msgid in self.issue_type_items],
        )
        self.choice_issue_type.SetName(_("Issue type"))
        self.choice_issue_type.SetToolTip(_("Choose the type of issue you want to report."))
        self.choice_issue_type.SetSelection(0)

        form_grid.Add(lbl_email, 0, wx.ALIGN_CENTER_VERTICAL)
        form_grid.Add(self.txt_email, 1, wx.EXPAND)
        form_grid.Add(lbl_issue, 0, wx.ALIGN_CENTER_VERTICAL)
        form_grid.Add(self.choice_issue_type, 0, wx.EXPAND)
        root.Add(form_grid, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        message_box = wx.StaticBoxSizer(wx.VERTICAL, panel, _("Describe your issue"))
        message_box.GetStaticBox().SetWindowStyle(message_box.GetStaticBox().GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        self.txt_user_message = wx.TextCtrl(panel, style=wx.TE_MULTILINE)
        self.txt_user_message.SetMinSize((-1, 180))
        self.txt_user_message.SetName(_("Describe your issue"))
        self.txt_user_message.SetToolTip(_("Describe the issue you want to report to support."))
        message_box.Add(self.txt_user_message, 1, wx.EXPAND | wx.ALL, 8)
        root.Add(message_box, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        details_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_toggle_technical = wx.Button(panel, label=_("Show technical details"))
        self.btn_toggle_technical.SetName(_("Show technical details"))
        details_actions.Add(self.btn_toggle_technical, 0)
        details_actions.AddStretchSpacer()
        root.Add(details_actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.technical_panel = wx.Panel(panel)
        technical_sizer = wx.StaticBoxSizer(wx.VERTICAL, self.technical_panel, _("Technical information"))
        technical_sizer.GetStaticBox().SetWindowStyle(technical_sizer.GetStaticBox().GetWindowStyle() & ~wx.TAB_TRAVERSAL)  # noqa: E501
        self.txt_technical_info = wx.TextCtrl(
            self.technical_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.txt_technical_info.SetMinSize((-1, 150))
        self.txt_technical_info.SetName(_("Technical information"))
        self.txt_technical_info.SetToolTip(_("Technical information that will be included in the report."))
        technical_sizer.Add(self.txt_technical_info, 1, wx.EXPAND | wx.ALL, 8)
        self.technical_panel.SetSizer(technical_sizer)
        self.technical_panel.Hide()
        root.Add(self.technical_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.fallback_panel = wx.Panel(panel)
        fallback_root = wx.BoxSizer(wx.VERTICAL)

        fallback_intro = wx.StaticText(
            self.fallback_panel,
            label=_(
                "The report could not be sent automatically. You can copy the details below and send them manually."
            ),
        )
        fallback_intro.Wrap(640)
        fallback_root.Add(fallback_intro, 0, wx.EXPAND | wx.ALL, 8)

        details_box = wx.StaticBoxSizer(wx.VERTICAL, self.fallback_panel, _("Support details"))
        details_box.GetStaticBox().SetWindowStyle(details_box.GetStaticBox().GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        details_grid = wx.FlexGridSizer(cols=3, vgap=8, hgap=8)
        details_grid.AddGrowableCol(1, 1)

        lbl_address = wx.StaticText(self.fallback_panel, label=_("Contact address"))
        self.txt_address = wx.TextCtrl(
            self.fallback_panel,
            value=self.contact_email,
            style=wx.TE_READONLY,
        )
        self.txt_address.SetName(_("Contact address"))
        self.btn_copy_address = wx.Button(self.fallback_panel, label=_("Copy address"))

        lbl_subject = wx.StaticText(self.fallback_panel, label=_("Subject"))
        self.txt_subject = wx.TextCtrl(self.fallback_panel, style=wx.TE_READONLY)
        self.txt_subject.SetName(_("Subject"))
        self.btn_copy_subject = wx.Button(self.fallback_panel, label=_("Copy subject"))

        details_grid.Add(lbl_address, 0, wx.ALIGN_CENTER_VERTICAL)
        details_grid.Add(self.txt_address, 1, wx.EXPAND)
        details_grid.Add(self.btn_copy_address, 0)
        details_grid.Add(lbl_subject, 0, wx.ALIGN_CENTER_VERTICAL)
        details_grid.Add(self.txt_subject, 1, wx.EXPAND)
        details_grid.Add(self.btn_copy_subject, 0)
        details_box.Add(details_grid, 0, wx.EXPAND | wx.ALL, 8)
        fallback_root.Add(details_box, 0, wx.EXPAND | wx.BOTTOM, 8)

        report_box = wx.StaticBoxSizer(wx.VERTICAL, self.fallback_panel, _("Report preview"))
        report_box.GetStaticBox().SetWindowStyle(report_box.GetStaticBox().GetWindowStyle() & ~wx.TAB_TRAVERSAL)
        self.txt_report_preview = wx.TextCtrl(
            self.fallback_panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY,
        )
        self.txt_report_preview.SetMinSize((-1, 180))
        self.txt_report_preview.SetName(_("Report preview"))
        self.txt_report_preview.SetToolTip(_("Full report text that can be copied manually if needed."))
        report_box.Add(self.txt_report_preview, 1, wx.EXPAND | wx.ALL, 8)
        fallback_root.Add(report_box, 1, wx.EXPAND | wx.BOTTOM, 8)

        fallback_actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_copy_report = wx.Button(self.fallback_panel, label=_("Copy report"))
        fallback_actions.Add(self.btn_copy_report, 0)
        fallback_actions.AddStretchSpacer()
        fallback_root.Add(fallback_actions, 0, wx.EXPAND)

        self.fallback_panel.SetSizer(fallback_root)
        self.fallback_panel.Hide()
        root.Add(self.fallback_panel, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        self.lbl_sending = wx.StaticText(panel, label="")
        self.lbl_sending.Hide()
        root.Add(self.lbl_sending, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        actions = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_send = wx.Button(panel, label=_("Send report"))
        self.btn_send.SetName(_("Send report"))
        self.btn_send.SetToolTip(_("Send the support report directly to support."))
        self.btn_send.SetDefault()
        self.btn_cancel = wx.Button(panel, wx.ID_CANCEL, label=_("Cancel"))
        self.btn_cancel.SetName(_("Cancel"))

        actions.AddStretchSpacer()
        actions.Add(self.btn_send, 0, wx.RIGHT, 8)
        actions.Add(self.btn_cancel, 0)
        root.Add(actions, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)

        panel.SetSizer(root)
        self.SetEscapeId(self.btn_cancel.GetId())
        self.SetAffirmativeId(self.btn_send.GetId())

        self.Bind(wx.EVT_TEXT, self.on_form_changed, self.txt_email)
        self.Bind(wx.EVT_TEXT, self.on_form_changed, self.txt_user_message)
        self.Bind(wx.EVT_CHOICE, self.on_issue_type_changed, self.choice_issue_type)
        self.Bind(wx.EVT_BUTTON, self.on_toggle_technical_details, self.btn_toggle_technical)
        self.Bind(wx.EVT_BUTTON, self.on_copy_address, self.btn_copy_address)
        self.Bind(wx.EVT_BUTTON, self.on_copy_subject, self.btn_copy_subject)
        self.Bind(wx.EVT_BUTTON, self.on_copy_report, self.btn_copy_report)
        self.Bind(wx.EVT_BUTTON, self.on_send_report, self.btn_send)
        self.Bind(wx.EVT_BUTTON, self.on_cancel, self.btn_cancel)
        self.Bind(wx.EVT_CLOSE, self.on_close_window)

        if self.saved_user_email:
            self.txt_email.SetValue(self.saved_user_email)
        self.txt_email.SetFocus()

    def _get_selected_issue_type(self):
        selection = self.choice_issue_type.GetSelection()
        if selection < 0 or selection >= len(self.issue_type_items):
            return self.issue_type_items[0][0]
        return self.issue_type_items[selection][0]

    def _clear_feedback(self):
        if self.lbl_feedback.IsShown():
            self.lbl_feedback.SetLabel("")
            self.lbl_feedback.Hide()
            self.Layout()

    def _refresh_generated_content(self):
        issue_type = self._get_selected_issue_type()
        self.generated_subject = build_support_subject(issue_type, self.support_context)
        self.generated_report = build_support_report(
            self.txt_email.GetValue(),
            issue_type,
            self.txt_user_message.GetValue(),
            self.support_context,
        )
        self.txt_subject.SetValue(self.generated_subject)
        self.txt_report_preview.SetValue(self.generated_report)
        self.txt_technical_info.SetValue(build_support_technical_block(self.support_context))

    def _set_feedback(self, message, is_error=False):
        self.lbl_feedback.SetLabel(message)
        self.lbl_feedback.SetForegroundColour(
            wx.Colour(180, 0, 0) if is_error else wx.Colour(0, 120, 0)
        )
        self.lbl_feedback.Show()
        self.Layout()
        wx.CallAfter(self.lbl_feedback.SetFocus)

    def _set_send_state(self, sending):
        self._send_in_progress = sending
        self.txt_email.Enable(not sending)
        self.choice_issue_type.Enable(not sending)
        self.txt_user_message.Enable(not sending)
        self.btn_toggle_technical.Enable(not sending)
        self.btn_send.Enable(not sending)
        self.btn_cancel.Enable(not sending)

        if sending:
            self.lbl_sending.SetLabel(_("Sending..."))
            self.lbl_sending.Show()
        else:
            self.lbl_sending.Hide()

        self.Layout()

    def _show_fallback_panel(self):
        self.fallback_panel.Show()
        self.Layout()
        self.FitInside()

    def _persist_user_email(self, email_address):
        cleaned_email = str(email_address or "").strip()
        if self.parent_window._settings.get("support_user_email", "") == cleaned_email:
            return
        self.parent_window._settings["support_user_email"] = cleaned_email
        self.parent_window._persist()

    def _toggle_technical_panel(self, show):
        self._technical_details_visible = bool(show)
        self.technical_panel.Show(self._technical_details_visible)
        label = _("Hide technical details") if self._technical_details_visible else _("Show technical details")
        self.btn_toggle_technical.SetLabel(label)
        self.btn_toggle_technical.SetName(label)
        self.Layout()

    def _copy_text(self, text, success_message):
        if not wx.TheClipboard.Open():
            self._set_feedback(_("Unable to copy to the clipboard."), is_error=True)
            return

        try:
            text_data = wx.TextDataObject()
            text_data.SetText(text)
            wx.TheClipboard.SetData(text_data)
        finally:
            wx.TheClipboard.Close()

        self._set_feedback(success_message)

    def _send_worker(self, email_address, issue_type, user_message, debug_log=""):
        try:
            send_support_report(
                email_address,
                issue_type,
                user_message,
                self.support_context,
                debug_log=debug_log,
            )
        except Exception as exc:  # noqa: BLE001
            logging.exception("Unable to send the support report.")
            wx.CallAfter(self._on_send_failure, str(exc))
            return

        wx.CallAfter(self._on_send_success)

    def _on_send_success(self):
        self._set_send_state(False)
        self._set_feedback(_("Your report has been sent successfully."))
        wx.CallLater(1200, self._close_after_success)

    def _on_send_failure(self, message):
        self._set_send_state(False)
        self._show_fallback_panel()
        self._set_feedback(message or _("Unable to send the support report right now."), is_error=True)

    def _close_after_success(self):
        if self and self.IsShown():
            self.EndModal(wx.ID_OK)

    def on_form_changed(self, event):
        self._clear_feedback()
        current_email = self.txt_email.GetValue().strip()
        if validate_support_email(current_email):
            self._persist_user_email(current_email)
        self._refresh_generated_content()
        event.Skip()

    def on_issue_type_changed(self, event):
        self._clear_feedback()
        self._refresh_generated_content()
        event.Skip()

    def on_toggle_technical_details(self, event):
        self._toggle_technical_panel(not self._technical_details_visible)

    def on_copy_address(self, event):
        self._copy_text(self.contact_email, _("Support email address copied to clipboard."))

    def on_copy_subject(self, event):
        self._copy_text(self.generated_subject, _("Support subject copied to clipboard."))

    def on_copy_report(self, event):
        self._copy_text(self.generated_report, _("Support report copied to clipboard."))

    def on_send_report(self, event):
        issue_type = self._get_selected_issue_type()
        email_address = self.txt_email.GetValue().strip()
        user_message = self.txt_user_message.GetValue()
        validation_message = validate_support_form(email_address, issue_type, user_message)
        if validation_message:
            self._set_feedback(validation_message, is_error=True)
            if not validate_support_email(email_address):
                self.txt_email.SetFocus()
                self.txt_email.SetSelection(-1, -1)
            elif not str(user_message or "").strip():
                self.txt_user_message.SetFocus()
                self.txt_user_message.SetSelection(-1, -1)
            return

        self._persist_user_email(email_address)
        self._refresh_generated_content()
        self._set_send_state(True)

        worker = threading.Thread(
            target=self._send_worker,
            args=(email_address, issue_type, user_message),
            daemon=True,
        )
        worker.start()

    def on_cancel(self, event):
        self.EndModal(wx.ID_CANCEL)

    def on_close_window(self, event):
        if self._send_in_progress:
            self._set_feedback(_("Please wait for the report to finish sending."), is_error=True)
            event.Veto()
            return
        event.Skip()
