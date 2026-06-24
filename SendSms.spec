# -*- mode: python ; coding: utf-8 -*-
import os
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None

# Collect brother_ql fully (data + submodules)
brother_datas, brother_binaries, brother_hiddenimports = collect_all('brother_ql')

# pywin32 DLLs — required for win32print (USB printer)
pywin32_dlls = []
for dll in ['pywintypes314.dll', 'pythoncom314.dll']:
    dll_path = os.path.join(sys.prefix, 'Lib', 'site-packages', 'pywin32_system32', dll)
    if os.path.exists(dll_path):
        pywin32_dlls.append((dll_path, '.'))

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=brother_binaries + pywin32_dlls,
    datas=[
        ('templates', 'templates'),
        ('static', 'static'),
    ] + brother_datas,
    hiddenimports=[
        # brother_ql
        'brother_ql',
        'brother_ql.backends',
        'brother_ql.backends.network',
        'brother_ql.backends.helpers',
        'brother_ql.backends.linux_kernel',
        'brother_ql.backends.pyusb',
        'brother_ql.conversion',
        'brother_ql.raster',
        'brother_ql.labels',
        'brother_ql.models',
        # pywin32
        'win32print',
        'win32api',
        'win32con',
        'pywintypes',
        # PIL
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'PIL.ImageFont',
        # Flask / web
        'flask',
        'jinja2',
        'jinja2.ext',
        'werkzeug',
        'werkzeug.serving',
        'click',
        'dotenv',
        'requests',
        'sqlite3',
        'webbrowser',
    ] + brother_hiddenimports + collect_submodules('win32'),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'scipy'],
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
    name='SendSMS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,   # Visa konsol — ändra till False när allt fungerar
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
