"""Папка с вариантами (банк D:\\Варианты): где искать файлы .variant и какие из них ещё не загружены.

Папка задаётся в «Настройках». Загружаются только новые файлы: уже загруженные узнаются по содержимому
(sha1 в таблице variant_files) или по названию варианта, которое уже есть в системе.
"""
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

# Папка по умолчанию. Если данные подменены (EGE_DATA_DIR — автотесты, проверочный запуск),
# настоящую папку не трогаем, пока её явно не укажут (или не задан EGE_VARIANTS_DIR).
DEFAULT_DIR = os.environ.get('EGE_VARIANTS_DIR', '' if os.environ.get('EGE_DATA_DIR') else r'D:\Варианты')
SETTING = 'variants_dir'
# служебные папки банка: кэш, окружение Python, git, «_исходники…» — не сканируются
SKIP_DIRS = {'.git', '.venv', 'кэш', '__pycache__', 'примеры проверки'}
MAX_FILES = 2000


def folder(get_setting):
    """Папка из настроек; если не задана — D:\\Варианты, когда она есть."""
    value = (get_setting(SETTING) or '').strip()
    if value:
        return value
    return DEFAULT_DIR if DEFAULT_DIR and os.path.isdir(DEFAULT_DIR) else ''


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_name(path):
    try:
        with zipfile.ZipFile(path) as archive:
            return str(json.loads(archive.read('manifest.json').decode('utf-8-sig')).get('name') or '')
    except Exception:
        return ''


def variant_files(root):
    """Все .variant в папке и подпапках (новые сверху), кроме служебных папок."""
    found = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(('_', '.'))]
        for name in files:
            if name.lower().endswith('.variant'):
                found.append(os.path.join(base, name))
                if len(found) >= MAX_FILES:
                    return found
    return sorted(found, key=lambda p: os.path.getmtime(p), reverse=True)


def new_files(root, known_hashes, known_names):
    """[(путь, sha1, название)] — файлы, которых ещё нет в системе."""
    result = []
    if not root or not os.path.isdir(root):
        return result
    for path in variant_files(root):
        name = manifest_name(path) or Path(path).stem
        if name in known_names:   # быстрый путь: такой вариант уже есть — файл даже не читаем целиком
            continue
        try:
            digest = sha1_of(path)
        except OSError:
            continue
        if digest in known_hashes:
            continue
        result.append((path, digest, name))
    return result


def pick_folder(start=''):
    """Окно Windows «Выбор папки» на компьютере учителя; '' — если отменили или окно недоступно."""
    if sys.platform != 'win32':
        return ''
    script = ('Add-Type -AssemblyName System.Windows.Forms;'
              '$d = New-Object System.Windows.Forms.FolderBrowserDialog;'
              '$d.Description = "Папка, где хранятся варианты (.variant)";'
              '$d.ShowNewFolderButton = $true;'
              f'$d.SelectedPath = "{start.replace(chr(34), "")}";'
              '$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true;'
              'if ($d.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) {'
              ' [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; Write-Output $d.SelectedPath }')
    try:
        done = subprocess.run(['powershell', '-NoProfile', '-STA', '-Command', script], capture_output=True,
                              timeout=600, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        return ''
    return done.stdout.decode('utf-8', 'replace').strip()


def open_in_explorer(path):
    if sys.platform == 'win32' and os.path.isdir(path):
        os.startfile(path)  # noqa: S606 — только на компьютере учителя
        return True
    return False
