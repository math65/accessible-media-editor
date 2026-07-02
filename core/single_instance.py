"""Relais de fichiers pour l'instance unique.

Quand l'utilisateur sélectionne plusieurs fichiers dans l'explorateur et lance
« Convertir avec Accessible Media Converter », Windows exécute le verbe une fois
par fichier — donc N processus. On garde une seule fenêtre (wx.SingleInstanceChecker
côté main.py) et les instances secondaires déposent leurs chemins dans un fichier
relais que l'instance maître draine via un wx.Timer.

Choix volontaire d'un fichier relais plutôt qu'un socket : aucune fenêtre pare-feu
Windows, priorité accessibilité.
"""

import os

from core.debug_session import ensure_config_dir, get_config_dir

RELAY_FILENAME = "pending_open.txt"


def get_relay_path():
    return os.path.join(get_config_dir(), RELAY_FILENAME)


def push_paths(paths):
    """Instance secondaire : ajoute les chemins (un par ligne) au relais."""
    valid = [p for p in paths if p]
    if not valid:
        return
    try:
        ensure_config_dir()
        with open(get_relay_path(), "a", encoding="utf-8") as handle:
            for path in valid:
                handle.write(path + "\n")
    except OSError:
        # Échec d'E/S transitoire : on abandonne silencieusement ce relais.
        pass


def drain_paths():
    """Instance maître : lit puis vide atomiquement le relais.

    Renomme le relais avant lecture pour que toute écriture concurrente reparte
    d'un fichier neuf (pas de perte entre lecture et suppression).
    """
    relay = get_relay_path()
    if not os.path.exists(relay):
        return []

    reading = relay + ".reading"
    try:
        os.replace(relay, reading)
    except OSError:
        # Une instance secondaire écrit peut-être à cet instant : on réessaiera
        # au prochain tic.
        return []

    paths = []
    try:
        with open(reading, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip()]
    except OSError:
        paths = []
    finally:
        try:
            os.remove(reading)
        except OSError:
            pass

    return paths
