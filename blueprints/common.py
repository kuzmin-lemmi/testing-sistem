"""Общие помощники для всех разделов: даты, проверка загрузок, CSRF, настройки оценок."""
import os
import secrets
import socket
import uuid
from datetime import datetime, timezone

from flask import jsonify, request, session

from config import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    APP_DIR,
    BASE_DIR,
    FROZEN,
    MAX_ATTACHMENT_SIZE,
    SHOW_ACCESS_CODE_OPTION,
    SHOW_EGE_BANKS,
    TEACHER_ALLOWED_IPS,
)
from models import TestSession, Variant, get_setting
from student_shortcut import local_addresses

PYODIDE_DIR = os.path.join(APP_DIR, 'pyodide') if FROZEN else os.path.join(BASE_DIR, 'static', 'pyodide')


def to_local_dt(value, with_seconds=False):
    """Преобразует UTC-время из БД в локальное время компьютера."""
    if not value:
        return ''
    try:
        raw = str(value).strip().replace('T', ' ')
        if raw.endswith('Z'):
            dt = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        else:
            dt = datetime.fromisoformat(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        local_dt = dt.astimezone()
        fmt = '%Y-%m-%d %H:%M:%S' if with_seconds else '%Y-%m-%d %H:%M'
        return local_dt.strftime(fmt)
    except Exception:
        text = str(value)
        return text[:19] if with_seconds else text[:16]


def get_local_ip():
    """Получить локальный IP-адрес"""
    addresses = local_addresses()  # работает и в школьной сети без выхода в интернет
    if addresses:
        return addresses[0]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return '127.0.0.1'


def allowed_file(filename, allowed_extensions):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_extensions


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _calc_remaining_seconds(student, test_session):
    time_limit = test_session['time_limit'] if test_session else 60
    extra_seconds = test_session.get('extra_seconds') or 0
    pause_total = test_session.get('pause_total_seconds') or 0
    paused = bool(test_session.get('paused'))
    paused_at = _parse_dt(test_session.get('paused_at'))

    start_time = _parse_dt(student.get('started_at')) or datetime.now(timezone.utc).replace(tzinfo=None)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    elapsed = (now - start_time).total_seconds()
    paused_elapsed = (now - paused_at).total_seconds() if paused and paused_at else 0
    # Считаем только паузу, прошедшую после входа ученика: его время идёт с нажатия «Начать».
    student_pause = max(0, pause_total + paused_elapsed - (student.get('pause_base_seconds') or 0))
    effective_elapsed = max(0, elapsed - student_pause)
    remaining = max(0, time_limit * 60 + extra_seconds - effective_elapsed)
    return int(remaining)


def _tasks_for_student(student, test_session):
    """Задания варианта в том порядке, в котором их видит ученик.

    Если учитель включил «задания вразброс», у каждого ученика свой порядок. Он зависит только
    от номера ученика, поэтому при обновлении страницы и повторном входе не меняется.
    """
    tasks = Variant.get_tasks(student['variant_id'])
    if test_session and test_session.get('shuffle_tasks') and len(tasks) > 1:
        import random
        random.Random(f"{student['session_id']}:{student['id']}").shuffle(tasks)
    return tasks


def _get_session_variant_options(session_id, include_task_counts=False):
    variants = TestSession.get_variants(session_id)
    for item in variants:
        item['label'] = str(item['position'])
        if include_task_counts:
            item['tasks_count'] = len(Variant.get_tasks(item['variant_id']))
    return variants


def _get_session_display_name(session_data):
    if not session_data:
        return 'Без варианта'
    if session_data.get('individual_mode'):
        return 'Индивидуальные варианты'

    session_variants = _get_session_variant_options(session_data['id'])
    if session_variants:
        labels = ', '.join(item['label'] for item in session_variants)
        return f'Несколько вариантов ({labels})'

    if session_data.get('variant_id'):
        variant = Variant.get_by_id(session_data['variant_id'])
        return variant['name'] if variant else 'Удалён'

    return 'Без варианта'


def _session_title(session_data):
    """«30.09 12:56 — Урок 29.09»: одинаковые варианты различаются датой."""
    return f"{to_local_dt(session_data.get('created_at'))[8:10]}.{to_local_dt(session_data.get('created_at'))[5:7]} " \
           f"{to_local_dt(session_data.get('created_at'))[11:16]} — {_get_session_display_name(session_data)}"


def generate_unique_filename(original_filename):
    """Генерирует уникальное имя файла"""
    ext = original_filename.rsplit('.', 1)[1].lower() if '.' in original_filename else ''
    return f"{uuid.uuid4().hex}.{ext}"


def _peek_bytes(file_obj, size):
    if not file_obj:
        return b''
    stream = getattr(file_obj, 'stream', None) or file_obj
    try:
        pos = stream.tell()
        data = stream.read(size)
        stream.seek(pos)
        return data
    except Exception:
        try:
            data = stream.read(size)
            if hasattr(stream, 'seek'):
                stream.seek(0)
            return data
        except Exception:
            return b''


def _get_file_size(file_obj):
    if not file_obj:
        return None
    stream = getattr(file_obj, 'stream', None) or file_obj
    try:
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(pos)
        return size
    except Exception:
        return None


def _validate_png(file_obj):
    return _peek_bytes(file_obj, 8) == b'\x89PNG\r\n\x1a\n'


def _validate_zip(file_obj):
    sig = _peek_bytes(file_obj, 4)
    return sig in (b'PK\x03\x04', b'PK\x05\x06')


def _validate_text(file_obj):
    sample = _peek_bytes(file_obj, 1024)
    return b'\x00' not in sample


def _validate_upload(file_obj, max_size, allowed_extensions, kind):
    if not file_obj or not file_obj.filename:
        return False, 'Пустой файл'

    if not allowed_file(file_obj.filename, allowed_extensions):
        return False, 'Недопустимое расширение файла'

    size = _get_file_size(file_obj)
    if size is not None and size > max_size:
        return False, 'Файл слишком большой'

    if kind == 'png' and not _validate_png(file_obj):
        return False, 'Файл не является PNG'
    if kind == 'zip' and not _validate_zip(file_obj):
        return False, 'Файл не является ZIP'
    if kind == 'xlsx' and not _validate_zip(file_obj):
        return False, 'Файл не является XLSX'
    if kind == 'pdf' and _peek_bytes(file_obj, 4) != b'%PDF':
        return False, 'Файл не является PDF'
    if kind in ('txt', 'csv') and not _validate_text(file_obj):
        return False, 'Файл не является текстовым'

    return True, None


def _validate_attachment_file(file_obj):
    ext = file_obj.filename.rsplit('.', 1)[1].lower() if file_obj and file_obj.filename and '.' in file_obj.filename else ''
    if ext in ('xlsx', 'ods', 'odt', 'docx', 'zip'):
        kind = 'xlsx'
    elif ext == 'pdf':
        kind = 'pdf'
    elif ext == 'csv':
        kind = 'csv'
    elif ext == 'xls':
        kind = 'bin'
    else:
        kind = 'txt'
    return _validate_upload(file_obj, MAX_ATTACHMENT_SIZE, ALLOWED_ATTACHMENT_EXTENSIONS, kind)


def _read_answers_from_form(form, answer_count, prefix='answer_', suffix=''):
    values = []
    for i in range(1, answer_count + 1):
        raw = form.get(f'{prefix}{i}{suffix}')
        values.append((raw or '').strip())
    return values


def _pack_answers_for_task(answer_values):
    clean = [(v or '').strip() for v in answer_values]
    answer_count = len(clean)
    answer_1 = clean[0] if answer_count >= 1 else None
    answer_2 = clean[1] if answer_count >= 2 else None
    extra = clean[2:] if answer_count > 2 else []
    answer_text = '\n'.join(extra) if extra else None
    return answer_1, answer_2, answer_text


def _task_answers_list(task):
    count = int(task.get('answer_count') or 1)
    if count < 1:
        count = 1
    values = []
    first = (task.get('answer_1') or '')
    second = (task.get('answer_2') or '')
    values.append(str(first).strip())
    if count >= 2:
        values.append(str(second).strip())
    if count > 2:
        extra = (task.get('answer_text') or '')
        extra_values = [line.strip() for line in str(extra).splitlines()]
        for idx in range(2, count):
            values.append(extra_values[idx - 2] if idx - 2 < len(extra_values) else '')
    return values


def _is_safe_stored_name(filename):
    if not filename:
        return False
    if os.path.isabs(filename):
        return False
    normalized = filename.replace('\\', '/').strip('/')
    if not normalized or '/' in normalized:
        return False
    if normalized in ('.', '..') or '..' in normalized:
        return False
    return normalized == os.path.basename(normalized)


def is_pyodide_available():
    return get_pyodide_base_url() is not None


def get_pyodide_base_url():
    direct = os.path.join(PYODIDE_DIR, 'pyodide.js')
    nested = os.path.join(PYODIDE_DIR, 'pyodide', 'pyodide.js')
    if os.path.exists(direct):
        return '/static/pyodide/'
    if os.path.exists(nested):
        return '/static/pyodide/pyodide/'
    return None


def _normalize_ip(value):
    if not value:
        return ''
    value = value.strip()
    if value.startswith('::ffff:'):
        return value[7:]
    return value


def _is_teacher_request_path(path):
    return not (
        path.startswith('/test') or
        path.startswith('/images/') or
        path.startswith('/attachments/') or
        path.startswith('/static/') or
        path == '/favicon.ico'
    )


def _request_from_teacher_machine():
    remote_ip = _normalize_ip(request.remote_addr)
    allowed = {_normalize_ip(ip) for ip in TEACHER_ALLOWED_IPS}
    return remote_ip in allowed


def _error_response(message, status_code):
    if request.path.startswith('/api/'):
        return jsonify({'error': message}), status_code
    return message, status_code


def _get_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


def _check_csrf():
    expected = session.get('_csrf_token', '')
    actual = request.headers.get('X-CSRFToken') or request.form.get('csrf_token', '')
    if not expected or not actual:
        return False
    return secrets.compare_digest(expected, actual)


def _clear_student_session():
    """Забывает ученика в этом браузере, но не трогает CSRF-токен учителя (иначе при проверке
    «учитель и ученик в одном браузере» кнопки учителя вдруг отвечают «Недействительный CSRF-токен»)."""
    for key in ('student_id', 'start_time', 'time_limit', 'app_mode'):
        session.pop(key, None)


DEFAULT_GRADE_PERCENTS = (85, 65, 45)  # «5», «4», «3» — от доли верных ответов


def show_ege_banks():
    """Банки заданий по номерам ЕГЭ: config/EGE_BANKS=1 или переключатель в «Настройках»."""
    return bool(SHOW_EGE_BANKS) or get_setting('show_ege_banks') == '1'


def show_access_code_option():
    return bool(SHOW_ACCESS_CODE_OPTION) or get_setting('show_access_code') == '1'


def grade_percents():
    try:
        values = tuple(int(get_setting(f'grade_{g}_percent', d)) for g, d in zip((5, 4, 3), DEFAULT_GRADE_PERCENTS))
        if all(1 <= v <= 100 for v in values):
            return values
    except (TypeError, ValueError):
        pass
    return DEFAULT_GRADE_PERCENTS
