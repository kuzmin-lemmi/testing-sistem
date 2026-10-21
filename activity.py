"""
Журнал событий «по-человечески» и технический журнал.

Для учителя: короткие понятные записи на главной странице («Подключился компьютер …», «Ученик … вошёл в тест»).
Для разработчика: подробный журнал в файле Данные/logs/server.log (его можно приложить к описанию ошибки).
Чёрного окна с запросами больше нет: всё, что раньше печаталось туда, пишется в файл.
"""
import logging
import logging.handlers
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

from config import LOG_DIR, PORT

FIREWALL_RULE = 'Тестирование ЕГЭ (ученики)'
LOG_FILE = os.path.join(LOG_DIR, 'server.log')

_events = deque(maxlen=300)
_lock = threading.Lock()
_names = {}            # ip -> (имя компьютера или None, когда узнали)
_resolving = set()
_seen_computers = set()
_last_student_contact = None
_started_at = time.time()
logger = logging.getLogger('testing')


# --- технический журнал ------------------------------------------------------

def setup_logging():
    """Всё, что раньше печаталось в чёрное окно, пишется в файл. Консоль не нужна (и может отсутствовать)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    for name in ('', 'werkzeug', 'testing'):
        target = logging.getLogger(name)
        target.handlers = [handler] if name in ('', ) else []
        target.setLevel(logging.INFO)
    logging.getLogger('werkzeug').propagate = True
    logging.getLogger('testing').propagate = True
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')


# --- имена компьютеров -------------------------------------------------------

def _resolve(ip):
    name = None
    try:
        name = socket.gethostbyaddr(ip)[0].split('.')[0] or None
    except (OSError, UnicodeError):
        pass
    with _lock:
        _names[ip] = (name, time.time())
        _resolving.discard(ip)


def computer_label(ip):
    """«компьютер КАБ-12 (192.168.0.12)» или просто «компьютер 192.168.0.12», если имя узнать не удалось."""
    if not ip:
        return 'неизвестный компьютер'
    if ip in ('127.0.0.1', '::1'):
        return 'этот компьютер (учителя)'
    with _lock:
        known = _names.get(ip)
        need = (known is None or time.time() - known[1] > 600) and ip not in _resolving
        if need:
            _resolving.add(ip)
    if need:
        threading.Thread(target=_resolve, args=(ip,), daemon=True).start()
    name = known[0] if known else None
    return f'компьютер {name} ({ip})' if name else f'компьютер {ip}'


# --- события для учителя -----------------------------------------------------

def log_event(text, kind='info', ip=None):
    """kind: info — обычное, ok — хорошее, warn — внимание, error — ошибка. {pc} в тексте заменится на название компьютера."""
    with _lock:
        _events.append({'time': time.time(), 'kind': kind, 'text': text, 'ip': ip})
    logger.log(logging.ERROR if kind == 'error' else logging.INFO, text.replace('{pc}', ip or '?'))


def recent_events(limit=40):
    with _lock:
        items = list(_events)[-limit:]
    result = []
    for item in reversed(items):
        text = item['text'].replace('{pc}', computer_label(item['ip']) if item['ip'] else 'компьютер')
        result.append({'time': datetime.fromtimestamp(item['time']).strftime('%H:%M:%S'), 'kind': item['kind'], 'text': text})
    return result


def note_student_contact(ip):
    """Вызывается на каждый запрос со страниц ученика. Пишет событие только когда компьютер появился впервые."""
    global _last_student_contact
    if not ip or ip in ('127.0.0.1', '::1'):
        return
    _last_student_contact = time.time()
    if ip not in _seen_computers:
        _seen_computers.add(ip)
        log_event('К системе подключился {pc}.', 'ok', ip)


def students_seen_count():
    return len(_seen_computers)


# --- сеть --------------------------------------------------------------------

def _no_window():
    return subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0


PORT_CHECK_SECONDS = 60
_port_check = {'open': None, 'at': 0.0, 'running': False}


def _own_rule_exists():
    """Своё правило: по коду возврата netsh (текст netsh бывает в UTF-8 или в CP866 — его не разбираем)."""
    try:
        result = subprocess.run(['netsh', 'advfirewall', 'firewall', 'show', 'rule', f'name={FIREWALL_RULE}'],
                                capture_output=True, timeout=8, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    return result.returncode == 0


def _check_port_rules():
    """Есть ли любое включённое правило, разрешающее входящие TCP на наш порт (например, созданное вручную).
    Перебор правил занимает несколько секунд, поэтому идёт в фоне, а результат хранится минуту."""
    script = (f"$r = Get-NetFirewallRule -Direction Inbound -Action Allow -Enabled True -ErrorAction SilentlyContinue | "
              f"Get-NetFirewallPortFilter -ErrorAction SilentlyContinue | "
              f"Where-Object {{ $_.Protocol -eq 'TCP' -and $_.LocalPort -contains '{PORT}' }}; "
              f"if ($r) {{ 'open' }} else {{ 'closed' }}")
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-Command', script], capture_output=True, timeout=60,
                                creationflags=_no_window())
        answer = (result.stdout or b'').decode('ascii', 'ignore').strip()
        _port_check['open'] = {'open': True, 'closed': False}.get(answer)
    except (OSError, subprocess.SubprocessError):
        _port_check['open'] = None
    finally:
        _port_check['at'] = time.time()
        _port_check['running'] = False


def firewall_status():
    """'ok' — для учеников открыт доступ, 'missing' — не открыт, 'unknown' — не Windows, не удалось проверить
    или проверка ещё идёт (тогда предупреждение не показываем)."""
    if sys.platform != 'win32':
        return 'unknown'
    own = _own_rule_exists()
    if own:
        return 'ok'
    if own is None:
        return 'unknown'
    if time.time() - _port_check['at'] > PORT_CHECK_SECONDS and not _port_check['running']:
        _port_check['running'] = True
        threading.Thread(target=_check_port_rules, daemon=True).start()
    return {True: 'ok', False: 'missing'}.get(_port_check['open'], 'unknown')


def open_firewall():
    """Добавляет правило брандмауэра Windows (только для своей локальной сети). Windows спросит разрешение у учителя."""
    if sys.platform != 'win32':
        return False
    # имя — в двойных кавычках отдельным аргументом: одинарные netsh не понимает и режет имя по пробелам
    arguments = ', '.join(f"'{item}'" for item in (
        'advfirewall', 'firewall', 'add', 'rule', f'name="{FIREWALL_RULE}"', 'dir=in', 'action=allow',
        'protocol=TCP', f'localport={PORT}', 'profile=any', 'remoteip=localsubnet'))
    command = f'Start-Process netsh -ArgumentList {arguments} -Verb RunAs -WindowStyle Hidden -Wait'
    try:
        subprocess.run(['powershell', '-NoProfile', '-Command', command], timeout=120, creationflags=_no_window())
    except (OSError, subprocess.SubprocessError):
        return False
    _port_check['at'] = 0.0  # перепроверить
    return firewall_status() == 'ok'


def uptime_text():
    seconds = int(time.time() - _started_at)
    return f'{seconds // 3600} ч {seconds % 3600 // 60} мин' if seconds >= 3600 else f'{seconds // 60} мин'
