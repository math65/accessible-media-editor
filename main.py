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

        # Nettoie un installeur de MAJ téléchargé lors d'une session précédente
        # (l'app a redémarré après une mise à jour). Best-effort, jamais bloquant.
        try:
            from core.updater import cleanup_update_artifacts
            cleanup_update_artifacts()
        except Exception:  # noqa: BLE001
            logging.exception("Échec du nettoyage des artefacts de mise à jour.")

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

        # Ouverture directe si un fichier est passé (verbe « Éditer avec… » / ligne de
        # commande) ; sinon on démarre sur l'écran d'accueil « aucun fichier » (Ctrl+O
        # ou Ctrl+V pour ouvrir). L'accueil maintient la boucle wx en vie.
        cli_paths = [path for path in sys.argv[1:] if os.path.exists(path)]
        opened = host.load_path(cli_paths[0]) if cli_paths else False
        if not opened:
            host.show_welcome()

        app.MainLoop()

    except Exception as exc:
        logging.critical("Erreur critique dans le main :", exc_info=True)
        raise exc

    logging.info("=== APPLICATION FERMÉE PROPREMENT ===")


if __name__ == '__main__':
    main()
