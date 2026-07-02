# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all


project_root = os.path.abspath(".")
version_file = os.environ.get("AME_VERSION_FILE") or None

# accessible_output2 charge à l'exécution des DLL de lecteurs d'écran (NVDA, JAWS…)
# depuis son dossier ; collect_all embarque ces données + sous-modules. On inclut
# aussi ses dépendances pures-Python pour fiabiliser le bundle.
_speech_datas, _speech_binaries, _speech_hidden = [], [], []
for _pkg in ('accessible_output2', 'platform_utils', 'libloader'):
    _d, _b, _h = collect_all(_pkg)
    _speech_datas += _d
    _speech_binaries += _b
    _speech_hidden += _h

# sounddevice (lecture audio de l'éditeur de segments) est un module simple qui
# charge la DLL PortAudio depuis le package de données séparé `_sounddevice_data`
# (chemin calculé à l'exécution : `_sounddevice_data/portaudio-binaries/...dll`).
# On collecte donc les DEUX, en conservant l'arborescence attendue par sounddevice.
_audio_datas, _audio_binaries, _audio_hidden = [], [], []
for _pkg in ('sounddevice', '_sounddevice_data'):
    _d, _b, _h = collect_all(_pkg)
    _audio_datas += _d
    _audio_binaries += _b
    _audio_hidden += _h


a = Analysis(
    ['main.py'],
    pathex=[project_root],
    binaries=[('bin\\ffmpeg.exe', 'bin'), ('bin\\ffprobe.exe', 'bin')] + _speech_binaries + _audio_binaries,
    datas=[('locales', 'locales')] + _speech_datas + _audio_datas,
    hiddenimports=['wx.richtext', 'wx._richtext', 'wx.xml', 'wx._xml',
                   'sounddevice', '_sounddevice_data',
                   'cffi', '_cffi_backend', 'pycparser'] + _speech_hidden + _audio_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='AccessibleMediaEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    version=version_file,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='AccessibleMediaEditor',
)
