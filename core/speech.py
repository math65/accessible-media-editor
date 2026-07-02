"""Retour vocal optionnel via un lecteur d'écran actif (NVDA, JAWS…).

Conçu pour ne JAMAIS casser l'UI :
- On n'utilise QUE des sorties lecteur d'écran à base de DLL. Toute sortie
  passant par COM est exclue (SAPI, mais aussi JAWS et Window-Eyes) : le chemin
  COM segfault sous Python 3.14 (crash natif non rattrapable). JAWS conserve la
  lecture native du focus, simplement pas l'annonce explicite.
- On ne parle que via un lecteur d'écran réellement ACTIF ; sinon, silence.
- Toute erreur d'init/de synthèse est avalée.
"""

import logging

# (module, classe) des sorties lecteur d'écran DLL (sans COM), par priorité.
_SCREEN_READER_OUTPUTS = (
    ('nvda', 'NVDA'),
    ('system_access', 'SystemAccess'),
    ('dolphin', 'Dolphin'),
    ('pc_talker', 'PCTalker'),
    ('zdsr', 'ZDSR'),
)

_speakers = None  # liste d'instances ; None tant que non initialisé


def _ensure_speakers():
    global _speakers
    if _speakers is not None:
        return
    _speakers = []
    for module_name, class_name in _SCREEN_READER_OUTPUTS:
        try:
            module = __import__(
                'accessible_output2.outputs.' + module_name, fromlist=[class_name]
            )
            _speakers.append(getattr(module, class_name)())
        except Exception:  # noqa: BLE001
            logging.debug("Sortie vocale %s indisponible.", module_name, exc_info=True)


def speak(message, interrupt=True):
    """Annonce un message via le premier lecteur d'écran actif.

    Silencieux si aucun lecteur d'écran n'est actif. `interrupt=True` coupe
    l'annonce précédente (utile pour des actions répétées, ex. réordonnancement)."""
    if not message:
        return
    _ensure_speakers()
    for speaker in _speakers:
        try:
            if speaker.is_active():
                speaker.speak(message, interrupt=interrupt)
                return
        except Exception:  # noqa: BLE001
            logging.debug("Échec d'annonce via %s.", type(speaker).__name__, exc_info=True)
