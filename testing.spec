# Сборка программы:  BUILD_EXE.bat  (создаёт папку dist\Тестирование с Тестирование.exe)
# -*- mode: python ; coding: utf-8 -*-
a = Analysis(
    ['launcher.py'],
    pathex=['.'],
    datas=[('templates', 'templates'), ('static/css', 'static/css'), ('static/js', 'static/js')],
    hiddenimports=['pystray._win32', 'PIL._tkinter_finder', 'activity', 'server', 'models', 'config',
                   'results_logic', 'localshot_import', 'student_shortcut'],
    excludes=['tkinter', 'matplotlib', 'numpy', 'scipy', 'pandas'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Тестирование', console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Тестирование', upx=False)
