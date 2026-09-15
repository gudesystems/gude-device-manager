import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

a = Analysis(
    ['upload.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('webui/index.html', 'webui'),
        ('webui/assets/*', 'webui/assets'),
    ],
    hiddenimports=(['requests', 'webui.server'] + collect_submodules('gude')),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name='gude-device-manager',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='gude/gude.ico',
)
