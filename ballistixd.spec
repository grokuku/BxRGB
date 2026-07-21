# -*- mode: python ; coding: utf-8 -*-
"""
ballistixd.spec — Configuration PyInstaller pour le daemon Ballistix RGB.

Build :
  cd <project_root>   (là où se trouve ce fichier .spec)
  pyinstaller ballistixd.spec --clean

Output :
  dist/ballistixd  (binaire standalone, ~15-20 MB)
"""

import os

# Répertoire racine du projet = dossier contenant ce .spec
# Utilisé pour pathex au lieu d'un chemin hardcodé.
SPEC_DIR = os.path.dirname(os.path.abspath(SPEC))

block_cipher = None

a = Analysis(
    [os.path.join(SPEC_DIR, 'ballistixd.py')],
    pathex=[SPEC_DIR],
    binaries=[],
    datas=[
        # Bundle le dossier static/ (frontend HTML/CSS/JS)
        ('static', 'static'),
    ],
    hiddenimports=[
        # smbus2
        'smbus2',
        # FastAPI / Uvicorn / Starlette
        'uvicorn',
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        'fastapi',
        'fastapi.staticfiles',
        'starlette',
        'starlette.staticfiles',
        'starlette.websockets',
        'pydantic',
        # Websockets (utilisé par le manager WS)
        'websockets',
        # Package ballistix
        'ballistix',
        'ballistix.core',
        'ballistix.detect',
        'ballistix.diagnostics',
        'ballistix.config',
        'ballistix.animations',
        'ballistix.server',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Exclure tkinter (pas besoin en mode web)
        'tkinter',
        'tkinter.ttk',
        'tkinter.colorchooser',
        'tkinter.messagebox',
        # Exclure les tests
        'pytest',
        'unittest',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ballistixd',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tempdir=None,
    console=True,  # Garde la console pour les logs systemd
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)