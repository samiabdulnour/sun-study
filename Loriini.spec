# -*- mode: python ; coding: utf-8 -*-
from pathlib import PurePath
from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.hooks import collect_all

datas = [('C:/Users/SamiAbdulnour/Documents/Adobe/Photoshop/sun-study/assets/loriini.ico', 'assets')]
binaries = []
hiddenimports = []
datas += collect_data_files('tzdata')
datas += collect_data_files('sun_study')
hiddenimports += collect_submodules('typer')
# The Rhino reader is a dev-only path and drags 6 MB of rhino3dm in.
hiddenimports += [m for m in collect_submodules('sun_study') if not m.startswith('sun_study.ingest.rhino')]
# ifcopenshell: the package and its wrapper, without the express rules, the
# MVD examples and the schema dumps nothing here imports -- 20 MB of the
# 106 MB it weighs when collected whole.
tmp_ret = collect_all('ifcopenshell')
_unwanted = ('/express/', '/mvd/', '/util/schema/')
datas += [d for d in tmp_ret[0] if not any(u in PurePath(d[0]).as_posix() for u in _unwanted)]
binaries += tmp_ret[1]
hiddenimports += [m for m in tmp_ret[2] if not m.startswith(('ifcopenshell.express', 'ifcopenshell.mvd', 'ifcopenshell.validate'))]


a = Analysis(
    ['C:/Users/SamiAbdulnour/Documents/Adobe/Photoshop/sun-study/src/sun_study/app/__main__.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PIL', 'rhino3dm', 'shapely', 'mypy', 'pydantic.v1.mypy', 'pydantic.mypy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Loriini',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='C:/Users/SamiAbdulnour/Documents/Adobe/Photoshop/sun-study/build/version_info.txt',
    icon=['C:/Users/SamiAbdulnour/Documents/Adobe/Photoshop/sun-study/assets/loriini.ico'],
)
