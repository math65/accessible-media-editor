# Accessible Media Editor

Éditeur audio/vidéo **accessible** (NVDA, 100 % clavier) pour les personnes aveugles
et malvoyantes, en `wxPython` avec `FFmpeg` embarqué.

Ce projet est **amorcé à partir du découpeur** d'[Accessible Media
Converter](https://github.com/math65/accessible-media-converter) : le modèle de
segments, le moteur audio (PortAudio/sounddevice) et l'export ont été repris tels
quels. L'ambition est d'en faire un vrai éditeur (au-delà de couper/retirer les pubs).

Voir **[CLAUDE.md](CLAUDE.md)** pour l'architecture, les pièges hérités et la feuille
de route.

## Lancer depuis les sources

```powershell
uv sync
uv run main.py
```

L'application ouvre un sélecteur de fichier au démarrage (ou accepte un chemin en
argument), puis affiche l'éditeur.
