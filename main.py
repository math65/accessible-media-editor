"""Accessible Media Editor — point d'entrée de l'application autonome.

L'application EST l'éditeur de segments (``ui/segment_editor.py``), amorcé à partir
du découpeur d'Accessible Media Converter. Le host (``ui/host.py``) tient lieu de
fenêtre principale invisible : il ouvre un fichier dans l'éditeur et pilote l'export.
"""

import logging
import os
import sys

import wx

from core.debug_session import load_raw_config
from core.i18n import AUTO_LANGUAGE_CODE, install_language
from core.logger import setup_logger


def init_i18n(config_data=None):
    logging.debug("Initialisation du système de traduction...")
    try:
        preferred_lang = AUTO_LANGUAGE_CODE
        if isinstance(config_data, dict):
            preferred_lang = config_data.get("ui_language", AUTO_LANGUAGE_CODE)
        lang_code, source = install_language(preferred_lang=preferred_lang, prefer_po=True)
        logging.info("Langue '%s' chargée depuis: %s.", lang_code, source)
    except Exception as exc:  # noqa: BLE001
        logging.warning("Échec du chargement de la langue : %s. Fallback anglais.", exc)


def main():
    raw_config = load_raw_config()
    setup_logger()

    try:
        init_i18n(raw_config)

        logging.info("Démarrage de wx.App...")
        app = wx.App(False)

        # Instance unique (le verbe « Ouvrir avec… » peut lancer plusieurs processus).
        # Pour ce seed, les instances secondaires sortent simplement.
        instance_checker = wx.SingleInstanceChecker("AccessibleMediaEditor")
        if instance_checker.IsAnotherRunning():
            logging.info("Instance déjà en cours, sortie.")
            return

        from ui.host import EditorHost
        host = EditorHost()

        cli_paths = [path for path in sys.argv[1:] if os.path.exists(path)]
        if cli_paths:
            opened = host.load_path(cli_paths[0])
        else:
            opened = host.open_file()

        if not opened or host.editor is None:
            logging.info("Aucun fichier ouvert, sortie.")
            return

        app.MainLoop()

    except Exception as exc:
        logging.critical("Erreur critique dans le main :", exc_info=True)
        raise exc

    logging.info("=== APPLICATION FERMÉE PROPREMENT ===")


if __name__ == '__main__':
    main()
