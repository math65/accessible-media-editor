"""Écran d'accueil « aucun fichier ouvert » de l'app autonome.

Affiché au démarrage (au lieu de forcer un sélecteur de fichier) et de nouveau
quand on ferme le fichier en cours. Depuis ici on ouvre un média via Ctrl+O
(sélecteur) ou Ctrl+V (coller un fichier copié dans l'Explorateur, ou un chemin
texte). Ouvrir un fichier bascule vers l'éditeur (le host masque cet accueil).

Accessibilité : contrôles wx natifs, un message texte lu par NVDA, focus initial
sur le bouton « Ouvrir un fichier… », raccourcis exposés via le menu (donc annoncés).
"""

import wx


class WelcomeFrame(wx.Frame):
    def __init__(self, on_open, on_paste):
        super().__init__(None, title=_("Accessible Media Editor"), size=(560, 320))
        self.on_open = on_open
        self.on_paste = on_paste

        self._build_menu()

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        heading = wx.StaticText(panel, label=_("No file open."))
        font = heading.GetFont()
        font.SetPointSize(font.GetPointSize() + 3)
        font.SetWeight(wx.FONTWEIGHT_BOLD)
        heading.SetFont(font)
        sizer.Add(heading, 0, wx.LEFT | wx.RIGHT | wx.TOP, 20)

        hint = wx.StaticText(panel, label=_(
            "Ctrl+O: open a media file.\n"
            "Ctrl+V: paste a file copied in the Explorer, or a file path."))
        sizer.Add(hint, 0, wx.ALL, 20)

        self.btn_open = wx.Button(panel, label=_("Open a file..."))
        self.btn_open.Bind(wx.EVT_BUTTON, lambda e: self.on_open())
        sizer.Add(self.btn_open, 0, wx.LEFT | wx.BOTTOM, 20)

        panel.SetSizer(sizer)
        self.CentreOnScreen()
        wx.CallAfter(self.btn_open.SetFocus)

    def _build_menu(self):
        bar = wx.MenuBar()
        m_file = wx.Menu()
        it_open = m_file.Append(wx.ID_OPEN, _("Open a file...") + "\tCtrl+O")
        it_paste = m_file.Append(wx.ID_PASTE, _("Paste a file or path") + "\tCtrl+V")
        m_file.AppendSeparator()
        it_quit = m_file.Append(wx.ID_EXIT, _("Quit") + "\tCtrl+Q")
        bar.Append(m_file, _("File"))
        self.SetMenuBar(bar)
        self.Bind(wx.EVT_MENU, lambda e: self.on_open(), it_open)
        self.Bind(wx.EVT_MENU, lambda e: self.on_paste(), it_paste)
        self.Bind(wx.EVT_MENU, lambda e: self.Close(), it_quit)
