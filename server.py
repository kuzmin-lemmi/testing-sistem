"""
Система тестирования ЕГЭ по информатике
Главный файл сервера
"""
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, send_file, flash, session
import os
import uuid
import socket
import secrets
from werkzeug.utils import secure_filename
from datetime import datetime, timezone

from config import (HOST, PORT, DATA_DIR, IMAGES_DIR, ATTACHMENTS_DIR, STUDENT_UPLOADS_DIR, EXPORTS_DIR,
                    MAX_IMAGE_SIZE, MAX_ATTACHMENT_SIZE, MAX_IMPORT_ZIP_SIZE,
                    MAX_STUDENT_UPLOAD_SIZE, ALLOWED_STUDENT_UPLOAD_EXTENSIONS,
                    ALLOWED_IMAGE_EXTENSIONS, ALLOWED_ATTACHMENT_EXTENSIONS,
                    DEFAULT_ANSWER_COUNT, SPECIAL_ANSWER_FORMAT,
                    SECRET_KEY, TEACHER_ALLOWED_IPS)
from models import init_db, migrate_db, Task, Variant, GradeCriteria, TestSession, Student, Answer, ClassGroup

app = Flask(__name__)
app.secret_key = SECRET_KEY


@app.after_request
def add_no_store_headers(response):
    if request.path.startswith('/test'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response

PYODIDE_DIR = os.path.join('static', 'pyodide')

@app.errorhandler(Exception)
def handle_exception(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error
    app.logger.exception('Unhandled exception')
    if request.path.startswith('/test'):
        return (
            "<!DOCTYPE html><html lang='ru'><head><meta charset='UTF-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1.0'>"
            "<title>Ошибка</title>"
            "<style>body{font-family:Arial,sans-serif;background:#f8fafc;color:#0f172a;"
            "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}"
            ".card{background:#fff;border-radius:16px;padding:28px;box-shadow:0 10px 25px rgba(0,0,0,0.1);max-width:520px;text-align:center;}"
            "h1{margin:0 0 12px;font-size:20px;}p{margin:0 0 6px;color:#475569;}</style>"
            "</head><body><div class='card'><h1>Произошла ошибка</h1>"
            "<p>Пожалуйста, сообщите учителю.</p>"
            "<p>Можно закрыть окно и войти заново.</p>"
            "</div></body></html>"
        ), 500
    return 'Internal Server Error', 500

# Инициализация БД при запуске
init_db()
migrate_db()


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


@app.template_filter('local_dt')
def local_dt_filter(value):
    return to_local_dt(value, with_seconds=False)


@app.template_filter('local_date')
def local_date_filter(value):
    local = to_local_dt(value, with_seconds=False)
    return local[:10] if local else ''

def get_local_ip():
    """Получить локальный IP-адрес"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
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
    effective_elapsed = max(0, elapsed - pause_total - paused_elapsed)
    remaining = max(0, time_limit * 60 + extra_seconds - effective_elapsed)
    return int(remaining)

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
    if kind in ('txt', 'csv') and not _validate_text(file_obj):
        return False, 'Файл не является текстовым'

    return True, None

def _validate_attachment_file(file_obj):
    ext = file_obj.filename.rsplit('.', 1)[1].lower() if file_obj and file_obj.filename and '.' in file_obj.filename else ''
    if ext in ('xlsx', 'ods'):
        kind = 'xlsx'
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

@app.context_processor
def inject_csrf_token():
    return {'csrf_token': _get_csrf_token}

@app.before_request
def protect_teacher_routes():
    if not _is_teacher_request_path(request.path):
        return None

    if not _request_from_teacher_machine():
        return _error_response('Доступ к панели учителя разрешён только с компьютера учителя', 403)

    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not _check_csrf():
        return _error_response('Недействительный CSRF-токен', 400)

    return None

# ==================== ГЛАВНАЯ СТРАНИЦА ====================

@app.route('/')
def index():
    """Главная страница учителя"""
    task_counts = Task.count_by_ege_number()
    total_tasks = sum(task_counts.values())
    variants_count = len(Variant.get_all())
    
    return render_template('teacher/index.html',
                         local_ip=get_local_ip(),
                         port=PORT,
                         total_tasks=total_tasks,
                         task_counts=task_counts,
                         variants_count=variants_count)

# ==================== БАНК ЗАДАЧ ====================

@app.route('/tasks')
def tasks_list():
    """Список всех задач по категориям"""
    mode = request.args.get('mode', 'ege')
    if mode not in ('ege', 'class'):
        mode = 'ege'

    task_counts = Task.count_by_ege_number()
    class_counts = Task.count_by_class()
    classes = ClassGroup.get_all()
    default_class_id = ClassGroup.get_default_id()
    selected_ege = request.args.get('ege', type=int)
    selected_class_id = request.args.get('class_id', type=int)

    # Старые ссылки mode=class показываем как "Общий банк"
    if mode == 'class' and selected_ege is None:
        selected_ege = 0
        mode = 'ege'

    tasks = []
    selected_class = None
    if mode == 'ege':
        if selected_ege == 0 and default_class_id:
            tasks = Task.get_by_class_id(default_class_id)
            selected_class = ClassGroup.get_by_id(default_class_id)
        elif selected_ege:
            tasks = Task.get_by_ege_number(selected_ege)
    else:
        if not selected_class_id:
            selected_class_id = default_class_id
        if selected_class_id:
            tasks = Task.get_by_class_id(selected_class_id)
            selected_class = ClassGroup.get_by_id(selected_class_id)

    general_bank_count = 0
    if default_class_id:
        for row in class_counts:
            if row.get('class_id') == default_class_id:
                general_bank_count = row.get('count', 0)
                break

    return render_template('teacher/tasks.html',
                          mode=mode,
                          task_counts=task_counts,
                          class_counts=class_counts,
                          classes=classes,
                          default_class_id=default_class_id,
                          general_bank_count=general_bank_count,
                          selected_ege=selected_ege,
                          selected_class_id=selected_class_id,
                          selected_class=selected_class,
                          tasks=tasks,
                          default_answer_count=DEFAULT_ANSWER_COUNT)


@app.route('/classes/add', methods=['POST'])
def class_add():
    """Добавление класса в справочник"""
    name = (request.form.get('class_name') or '').strip().upper()
    if not name:
        flash('Введите название класса (например, 7А)', 'error')
        return redirect(url_for('tasks_list', mode='class'))
    try:
        class_id = ClassGroup.create(name)
        flash(f'Класс {name} добавлен', 'success')
        return redirect(url_for('tasks_list', mode='class', class_id=class_id))
    except Exception:
        flash('Такой класс уже есть', 'warning')
        return redirect(url_for('tasks_list', mode='class'))


@app.route('/tasks/move-to-class', methods=['POST'])
def tasks_move_to_class():
    """Перенос задач из ЕГЭ-раздела в классный раздел"""
    class_id = request.form.get('class_id', type=int)
    task_ids = [int(tid) for tid in request.form.getlist('task_ids') if str(tid).isdigit()]
    ege_back = request.form.get('ege', type=int, default=1)
    origin_mode = request.form.get('origin_mode', 'ege')
    origin_class_id = request.form.get('origin_class_id', type=int)
    if not class_id:
        flash('Выберите класс для переноса', 'error')
        if origin_mode == 'class' and origin_class_id:
            return redirect(url_for('tasks_list', mode='class', class_id=origin_class_id))
        return redirect(url_for('tasks_list', mode='ege', ege=ege_back))
    if not task_ids:
        flash('Выберите хотя бы одну задачу', 'warning')
        if origin_mode == 'class' and origin_class_id:
            return redirect(url_for('tasks_list', mode='class', class_id=origin_class_id))
        return redirect(url_for('tasks_list', mode='ege', ege=ege_back))
    moved = Task.move_to_class(task_ids, class_id)
    flash(f'Перенесено задач: {moved}', 'success')
    if origin_mode == 'class' and origin_class_id:
        return redirect(url_for('tasks_list', mode='class', class_id=origin_class_id))
    return redirect(url_for('tasks_list', mode='ege', ege=ege_back))


@app.route('/tasks/move-bank', methods=['POST'])
def tasks_move_bank():
    """Перемещение одной задачи drag-and-drop в нужный банк"""
    task_id = request.form.get('task_id', type=int)
    target = (request.form.get('target') or '').strip().lower()
    target_ege = request.form.get('target_ege', type=int)
    return_ege = request.form.get('return_ege', type=int)

    if return_ege is None:
        return_ege = 1

    if not task_id:
        flash('Не выбрана задача для переноса', 'error')
        return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

    if target == 'general':
        class_id = ClassGroup.get_default_id()
        Task.update(task_id, task_scope='class', class_id=class_id)
        flash(f'Задача #{task_id} перемещена в общий банк', 'success')
    elif target == 'ege':
        if not target_ege or target_ege < 1 or target_ege > 27:
            flash('Некорректный номер банка ЕГЭ', 'error')
            return redirect(url_for('tasks_list', mode='ege', ege=return_ege))
        Task.update(task_id, task_scope='ege', ege_number=target_ege, class_id=None)
        flash(f'Задача #{task_id} перемещена в ЕГЭ №{target_ege}', 'success')
    else:
        flash('Неизвестный целевой банк', 'error')

    return redirect(url_for('tasks_list', mode='ege', ege=return_ege))


@app.route('/tasks/bulk-action', methods=['POST'])
def tasks_bulk_action():
    """Массовые операции: удаление или перенос в нужный банк"""
    action = (request.form.get('action') or '').strip().lower()
    task_ids = [int(tid) for tid in request.form.getlist('task_ids') if str(tid).isdigit()]
    return_ege = request.form.get('return_ege', type=int)
    if return_ege is None:
        return_ege = 1

    if not task_ids:
        flash('Отметьте хотя бы одну задачу', 'warning')
        return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

    if action == 'delete':
        deleted = 0
        for task_id in task_ids:
            task = Task.get_by_id(task_id)
            if not task:
                continue
            image_path = os.path.join(IMAGES_DIR, task['image_path'])
            if os.path.exists(image_path):
                os.remove(image_path)
            if task.get('attachment_path'):
                attachment_path = os.path.join(ATTACHMENTS_DIR, task['attachment_path'])
                if os.path.exists(attachment_path):
                    os.remove(attachment_path)
            Task.delete(task_id)
            deleted += 1
        flash(f'Удалено задач: {deleted}', 'success')
        return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

    if action == 'move':
        target = (request.form.get('target') or '').strip().lower()
        target_ege = request.form.get('target_ege', type=int)

        moved = 0
        if target == 'general':
            class_id = ClassGroup.get_default_id()
            for task_id in task_ids:
                Task.update(task_id, task_scope='class', class_id=class_id)
                moved += 1
            flash(f'Перемещено в общий банк: {moved}', 'success')
            return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

        if target == 'ege':
            if not target_ege or target_ege < 1 or target_ege > 27:
                flash('Выберите номер ЕГЭ для переноса', 'error')
                return redirect(url_for('tasks_list', mode='ege', ege=return_ege))
            for task_id in task_ids:
                Task.update(task_id, task_scope='ege', ege_number=target_ege, class_id=None)
                moved += 1
            flash(f'Перемещено в ЕГЭ №{target_ege}: {moved}', 'success')
            return redirect(url_for('tasks_list', mode='ege', ege=target_ege))

        flash('Выберите целевой банк для переноса', 'error')
        return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

    flash('Неизвестное действие', 'error')
    return redirect(url_for('tasks_list', mode='ege', ege=return_ege))

@app.route('/tasks/add', methods=['GET', 'POST'])
def task_add():
    """Добавление новой задачи"""
    mode = request.args.get('mode', request.form.get('mode', 'ege'))
    if mode not in ('ege', 'class'):
        mode = 'ege'

    if request.method == 'POST':
        class_id = request.form.get('class_id', type=int)
        if mode == 'class':
            if not class_id:
                flash('Выберите класс', 'error')
                return redirect(url_for('task_add', mode='class'))
            ege_number = 1
        else:
            ege_number = int(request.form.get('ege_number'))
        
        # Обработка изображения
        image = request.files.get('image')
        if not image or not image.filename:
            flash('Необходимо загрузить PNG-изображение', 'error')
            return redirect(url_for('task_add', mode=mode, ege=ege_number, class_id=class_id))
        valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
        if not valid_image:
            flash(image_error or 'Необходимо загрузить PNG-изображение', 'error')
            return redirect(url_for('task_add', mode=mode, ege=ege_number, class_id=class_id))
        
        image_filename = generate_unique_filename(image.filename)
        image_path = os.path.join(IMAGES_DIR, image_filename)
        image.save(image_path)
        
        # Обработка прикреплённого файла (опционально)
        attachment_path = None
        attachment_filename = None
        attachment_name = None
        attachment = request.files.get('attachment')
        if attachment and attachment.filename:
            valid_attachment, attachment_error = _validate_attachment_file(attachment)
            if not valid_attachment:
                flash(attachment_error or 'Недопустимый файл данных', 'error')
                return redirect(url_for('task_add', mode=mode, ege=ege_number, class_id=class_id))
            attachment_name = secure_filename(attachment.filename)
            attachment_filename = generate_unique_filename(attachment.filename)
            attachment_path = os.path.join(ATTACHMENTS_DIR, attachment_filename)
            attachment.save(attachment_path)
        
        answer_kind = request.form.get('answer_kind', 'classic')
        if answer_kind not in ('classic', 'file_upload'):
            answer_kind = 'classic'

        if answer_kind == 'file_upload':
            answer_count = 0
            answer_1 = answer_2 = answer_text = None
        else:
            answer_count = request.form.get('answer_count', type=int, default=1)
            if not answer_count or answer_count < 1:
                answer_count = 1
            if answer_count > 20:
                answer_count = 20

            answer_values = _read_answers_from_form(request.form, answer_count)
            if any((v or '').strip() == '' for v in answer_values):
                flash('Заполните все ответы для задачи', 'error')
                return redirect(url_for('task_add', mode=mode, ege=ege_number, class_id=class_id))

            answer_1, answer_2, answer_text = _pack_answers_for_task(answer_values)

        Task.create(
            ege_number=ege_number,
            image_path=image_filename,
            answer_kind=answer_kind,
            answer_1=answer_1,
            answer_count=answer_count,
            answer_2=answer_2,
            attachment_path=attachment_filename if attachment_path else None,
            attachment_name=attachment_name,
            answer_text=answer_text,
            task_scope=mode,
            class_id=class_id if mode == 'class' else None,
        )

        if mode == 'class':
            group = ClassGroup.get_by_id(class_id)
            flash(f'Задача для класса {group["name"] if group else class_id} успешно добавлена', 'success')
            return redirect(url_for('tasks_list', mode='class', class_id=class_id))
        flash(f'Задача для номера {ege_number} успешно добавлена', 'success')
        return redirect(url_for('tasks_list', mode='ege', ege=ege_number))

    ege_number = request.args.get('ege', type=int, default=1)
    class_id = request.args.get('class_id', type=int)
    if mode == 'class' and not class_id:
        class_id = ClassGroup.get_default_id()
    return render_template('teacher/task_add.html',
                          mode=mode,
                          ege_number=ege_number,
                          class_id=class_id,
                          classes=ClassGroup.get_all(),
                          default_answer_count=DEFAULT_ANSWER_COUNT,
                          special_format=SPECIAL_ANSWER_FORMAT)

@app.route('/tasks/<int:task_id>')
def task_view(task_id):
    """Просмотр задачи"""
    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('tasks_list'))
    return render_template('teacher/task_view.html', task=task)

@app.route('/tasks/<int:task_id>/edit', methods=['GET', 'POST'])
def task_edit(task_id):
    """Редактирование задачи"""
    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('tasks_list'))
    
    if request.method == 'POST':
        updates = {}

        mode = request.form.get('mode', task.get('task_scope') or 'ege')
        if mode not in ('ege', 'class'):
            mode = 'ege'
        updates['task_scope'] = mode
        if mode == 'class':
            class_id = request.form.get('class_id', type=int)
            if not class_id:
                flash('Выберите класс', 'error')
                return redirect(url_for('task_edit', task_id=task_id))
            updates['class_id'] = class_id
        else:
            ege_number = request.form.get('ege_number')
            if ege_number:
                updates['ege_number'] = int(ege_number)
            updates['class_id'] = None
        
        answer_kind = request.form.get('answer_kind', 'classic')
        if answer_kind not in ('classic', 'file_upload'):
            answer_kind = 'classic'
        updates['answer_kind'] = answer_kind

        if answer_kind == 'file_upload':
            updates['answer_count'] = 0
            updates['answer_1'] = updates['answer_2'] = updates['answer_text'] = None
        else:
            answer_count = request.form.get('answer_count', type=int, default=1)
            if not answer_count or answer_count < 1:
                answer_count = 1
            if answer_count > 20:
                answer_count = 20
            answer_values = _read_answers_from_form(request.form, answer_count)
            if any((v or '').strip() == '' for v in answer_values):
                flash('Заполните все ответы для задачи', 'error')
                return redirect(url_for('task_edit', task_id=task_id))

            updates['answer_count'] = answer_count
            updates['answer_1'], updates['answer_2'], updates['answer_text'] = _pack_answers_for_task(answer_values)
        
        # Обновление изображения (если загружено новое)
        image = request.files.get('image')
        if image and image.filename:
            valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
            if not valid_image:
                flash(image_error or 'Необходимо загрузить PNG-изображение', 'error')
                return redirect(url_for('task_edit', task_id=task_id))
            # Удаляем старое изображение
            old_image_path = os.path.join(IMAGES_DIR, task['image_path'])
            if os.path.exists(old_image_path):
                os.remove(old_image_path)
            
            image_filename = generate_unique_filename(image.filename)
            image.save(os.path.join(IMAGES_DIR, image_filename))
            updates['image_path'] = image_filename
        
        # Обработка прикреплённого файла
        if request.form.get('remove_attachment') == '1':
            # Удаление прикреплённого файла
            if task['attachment_path']:
                old_attachment = os.path.join(ATTACHMENTS_DIR, task['attachment_path'])
                if os.path.exists(old_attachment):
                    os.remove(old_attachment)
            updates['attachment_path'] = None
            updates['attachment_name'] = None
        else:
            attachment = request.files.get('attachment')
            if attachment and attachment.filename:
                valid_attachment, attachment_error = _validate_attachment_file(attachment)
                if not valid_attachment:
                    flash(attachment_error or 'Недопустимый файл данных', 'error')
                    return redirect(url_for('task_edit', task_id=task_id))

                # Удаляем старый файл
                if task['attachment_path']:
                    old_attachment = os.path.join(ATTACHMENTS_DIR, task['attachment_path'])
                    if os.path.exists(old_attachment):
                        os.remove(old_attachment)
                
                attachment_name = secure_filename(attachment.filename)
                attachment_filename = generate_unique_filename(attachment.filename)
                attachment.save(os.path.join(ATTACHMENTS_DIR, attachment_filename))
                updates['attachment_path'] = attachment_filename
                updates['attachment_name'] = attachment_name
        
        Task.update(task_id, **updates)
        flash('Задача успешно обновлена', 'success')
        if updates.get('task_scope') == 'class':
            return redirect(url_for('tasks_list', mode='class', class_id=updates.get('class_id')))
        return redirect(url_for('tasks_list', mode='ege', ege=updates.get('ege_number', task['ege_number'])))
    
    return render_template('teacher/task_edit.html', 
                          task=task,
                          classes=ClassGroup.get_all(),
                          existing_answers=_task_answers_list(task),
                          default_answer_count=DEFAULT_ANSWER_COUNT)

@app.route('/tasks/<int:task_id>/delete', methods=['POST'])
def task_delete(task_id):
    """Удаление задачи"""
    return_ege = request.form.get('return_ege', type=int)
    if return_ege is None:
        return_ege = 1

    task = Task.get_by_id(task_id)
    if task:
        # Удаляем файлы
        image_path = os.path.join(IMAGES_DIR, task['image_path'])
        if os.path.exists(image_path):
            os.remove(image_path)
        if task['attachment_path']:
            attachment_path = os.path.join(ATTACHMENTS_DIR, task['attachment_path'])
            if os.path.exists(attachment_path):
                os.remove(attachment_path)
        
        Task.delete(task_id)
        flash('Задача удалена', 'success')

    return redirect(url_for('tasks_list', mode='ege', ege=return_ege))


@app.route('/tasks/bulk', methods=['GET', 'POST'])
def tasks_bulk_upload():
    """Массовая загрузка задач"""
    mode = request.args.get('mode', request.form.get('mode', 'ege'))
    if mode not in ('ege', 'class'):
        mode = 'ege'

    class_id = request.args.get('class_id', type=int)
    if class_id is None:
        class_id = request.form.get('class_id', type=int)

    if mode == 'class' and not class_id:
        class_id = ClassGroup.get_default_id()

    if request.method == 'POST':
        if mode == 'class':
            ege_number = 1
            if not class_id:
                flash('Не найден общий банк для загрузки', 'error')
                return redirect(url_for('tasks_list', mode='ege', ege=0))
        else:
            ege_number = int(request.form.get('ege_number'))
        
        index = 0
        created = 0
        while True:
            image_path = request.form.get(f'image_path_{index}')
            if not image_path:
                break

            answer_count = request.form.get(f'answer_count_{index}', type=int, default=1)
            if not answer_count or answer_count < 1:
                answer_count = 1
            if answer_count > 20:
                answer_count = 20
            answer_values = _read_answers_from_form(request.form, answer_count, suffix=f'_{index}')
            if any((v or '').strip() == '' for v in answer_values):
                flash(f'Пропущены ответы у строки #{index + 1}', 'warning')
                index += 1
                continue

            answer_1, answer_2, answer_text = _pack_answers_for_task(answer_values)

            attachment_filename = None
            attachment_name = None
            attachment = request.files.get(f'attachment_{index}')
            if attachment and attachment.filename:
                valid_attachment, attachment_error = _validate_attachment_file(attachment)
                if not valid_attachment:
                    flash(f'Строка #{index + 1}: {attachment_error or "Недопустимый файл данных"}', 'warning')
                    index += 1
                    continue
                attachment_name = secure_filename(attachment.filename)
                attachment_filename = generate_unique_filename(attachment.filename)
                attachment.save(os.path.join(ATTACHMENTS_DIR, attachment_filename))
            
            Task.create(
                ege_number=ege_number,
                image_path=image_path,
                answer_1=answer_1,
                answer_2=answer_2,
                answer_count=answer_count,
                answer_text=answer_text,
                attachment_path=attachment_filename,
                attachment_name=attachment_name,
                task_scope='class' if mode == 'class' else 'ege',
                class_id=class_id if mode == 'class' else None,
            )
            index += 1
            created += 1

        if mode == 'class':
            flash(f'Добавлено {created} задач в общий банк', 'success')
            return redirect(url_for('tasks_list', mode='ege', ege=0))

        flash(f'Добавлено {created} задач для номера {ege_number}', 'success')
        return redirect(url_for('tasks_list', mode='ege', ege=ege_number))
    
    # GET - показываем форму
    ege_number = request.args.get('ege', type=int, default=1)
    return render_template('teacher/tasks_bulk.html',
                          ege_number=ege_number,
                          mode=mode,
                          class_id=class_id,
                          default_answer_count=DEFAULT_ANSWER_COUNT,
                          special_format=SPECIAL_ANSWER_FORMAT)


@app.route('/api/upload-image', methods=['POST'])
def api_upload_image():
    """API для загрузки изображения (AJAX)"""
    if 'image' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    
    image = request.files['image']
    valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
    if not valid_image:
        return jsonify({'error': image_error or 'Недопустимый файл'}), 400
    
    image_filename = generate_unique_filename(image.filename)
    image_path = os.path.join(IMAGES_DIR, image_filename)
    image.save(image_path)
    
    return jsonify({
        'success': True,
        'filename': image_filename,
        'url': url_for('serve_image', filename=image_filename)
    })


@app.route('/api/delete-image', methods=['POST'])
def api_delete_image():
    """API для удаления загруженного изображения"""
    data = request.get_json()
    filename = data.get('filename')
    
    if filename:
        image_path = os.path.join(IMAGES_DIR, filename)
        if os.path.exists(image_path):
            os.remove(image_path)
            return jsonify({'success': True})
    
    return jsonify({'error': 'Файл не найден'}), 404

# ==================== ФАЙЛЫ ====================

@app.route('/images/<filename>')
def serve_image(filename):
    """Отдача изображений"""
    return send_from_directory(IMAGES_DIR, filename)

@app.route('/attachments/<filename>')
def serve_attachment(filename):
    """Отдача прикреплённых файлов"""
    return send_from_directory(ATTACHMENTS_DIR, filename, as_attachment=True)


# ==================== ЗАГРУЗКА ФАЙЛОВ УЧЕНИКОВ ====================

@app.route('/test/upload-answer-file', methods=['POST'])
def student_upload_answer_file():
    """Ученик загружает файл (.ods/.odt) как ответ на задание типа file_upload."""
    from flask import session as flask_session
    student_id = flask_session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Не авторизован'}), 401

    student = Student.get_by_id(student_id)
    if not student:
        return jsonify({'error': 'Ученик не найден'}), 401

    test_session = TestSession.get_by_id(student['session_id'])
    if not test_session or test_session['status'] != 'active':
        return jsonify({'error': 'Сессия неактивна'}), 403
    if test_session.get('paused'):
        return jsonify({'error': 'Тестирование на паузе'}), 403

    task_id = request.form.get('task_id', type=int)
    if not task_id:
        return jsonify({'error': 'Не указан task_id'}), 400

    # Проверяем, что задача входит в вариант ученика
    tasks = Variant.get_tasks(student['variant_id'])
    task_ids_in_variant = {t['id'] for t in tasks}
    if task_id not in task_ids_in_variant:
        return jsonify({'error': 'Задача не принадлежит вашему варианту'}), 403

    task = Task.get_by_id(task_id)
    if not task or task.get('answer_kind') != 'file_upload':
        return jsonify({'error': 'Для этой задачи загрузка файла не предусмотрена'}), 400

    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': 'Файл не выбран'}), 400

    original_name = uploaded.filename
    ext = original_name.rsplit('.', 1)[-1].lower() if '.' in original_name else ''
    if ext not in ALLOWED_STUDENT_UPLOAD_EXTENSIONS:
        return jsonify({'error': f'Разрешены только файлы: {", ".join(sorted(ALLOWED_STUDENT_UPLOAD_EXTENSIONS))}'}), 400

    # Читаем в память для проверки размера
    data = uploaded.read()
    if len(data) > MAX_STUDENT_UPLOAD_SIZE:
        return jsonify({'error': f'Файл слишком большой (максимум {MAX_STUDENT_UPLOAD_SIZE // 1024 // 1024} МБ)'}), 400
    if len(data) == 0:
        return jsonify({'error': 'Файл пустой'}), 400

    # Удаляем старый файл если был
    old_answer = Answer.get_for_student_task(student_id, task_id)
    if old_answer and old_answer.get('upload_path'):
        old_path = os.path.join(STUDENT_UPLOADS_DIR, old_answer['upload_path'])
        if os.path.isfile(old_path):
            try:
                os.remove(old_path)
            except OSError:
                pass

    # Сохраняем новый файл
    stored_name = f"{uuid.uuid4().hex}.{ext}"
    dest = os.path.join(STUDENT_UPLOADS_DIR, stored_name)
    with open(dest, 'wb') as f:
        f.write(data)

    safe_original = secure_filename(original_name) or stored_name
    Answer.save_upload(student_id, task_id, stored_name, safe_original, len(data))
    Student.touch(student_id)

    return jsonify({
        'success': True,
        'upload_name': safe_original,
        'upload_size': len(data),
    })


# ==================== СКАЧИВАНИЕ И ПРОВЕРКА ФАЙЛОВ УЧЕНИКОВ (УЧИТЕЛЬ) ====================

@app.route('/teacher/answers/download/<int:student_id>/<int:task_id>')
def teacher_download_answer_file(student_id, task_id):
    """Учитель скачивает файл, загруженный учеником."""
    answer = Answer.get_for_student_task(student_id, task_id)
    if not answer or not answer.get('upload_path'):
        return 'Файл не найден', 404
    stored = answer['upload_path']
    original = answer.get('upload_name') or stored
    return send_from_directory(STUDENT_UPLOADS_DIR, stored,
                               as_attachment=True,
                               download_name=original)


@app.route('/teacher/answers/mark', methods=['POST'])
def teacher_mark_answer():
    """Учитель вручную ставит ✓ или ✗ на file_upload задаче."""
    data = request.get_json(silent=True) or {}
    student_id = data.get('student_id')
    task_id = data.get('task_id')
    is_correct = data.get('is_correct')  # True / False

    if student_id is None or task_id is None or is_correct is None:
        return jsonify({'error': 'Неполные данные'}), 400

    Answer.mark(int(student_id), int(task_id), bool(is_correct))
    return jsonify({'success': True})

# ==================== API ====================

@app.route('/api/tasks/count')
def api_tasks_count():
    """API: количество задач по номерам ЕГЭ"""
    return jsonify(Task.count_by_ege_number())

@app.route('/api/tasks/<int:ege_number>')
def api_tasks_by_ege(ege_number):
    """API: задачи по номеру ЕГЭ"""
    tasks = Task.get_by_ege_number(ege_number)
    return jsonify(tasks)

# ==================== ВАРИАНТЫ ====================

@app.route('/variants')
def variants_list():
    """Список вариантов"""
    scope = request.args.get('scope', 'all')
    selected_variant_id = request.args.get('variant_id', type=int)
    variants = Variant.get_all(None if scope == 'all' else scope)
    classes_by_id = {c['id']: c for c in ClassGroup.get_all()}
    # Добавляем количество задач в каждом варианте
    for v in variants:
        v['tasks_count'] = len(Variant.get_tasks(v['id']))
        if v.get('variant_scope') == 'class' and v.get('class_id'):
            group = classes_by_id.get(v['class_id'])
            v['class_name'] = group['name'] if group else f"#{v['class_id']}"

    selected_variant = None
    selected_tasks = []
    if variants:
        ids = {v['id'] for v in variants}
        if not selected_variant_id or selected_variant_id not in ids:
            selected_variant_id = variants[0]['id']
        selected_variant = next((v for v in variants if v['id'] == selected_variant_id), None)
        if selected_variant:
            selected_tasks = Variant.get_tasks(selected_variant_id)

    return render_template('teacher/variants.html',
                          variants=variants,
                          scope=scope,
                          selected_variant=selected_variant,
                          selected_variant_id=selected_variant_id,
                          selected_tasks=selected_tasks)

@app.route('/variants/create', methods=['GET', 'POST'])
def variant_create():
    """Создание варианта"""
    scope = request.args.get('scope', 'ege')
    if scope not in ('ege', 'class'):
        scope = 'ege'
    
    if request.method == 'POST':
        name = request.form.get('name')
        variant_type = request.form.get('variant_type')
        generation_mode = request.form.get('generation_mode')
        variant_scope = request.form.get('variant_scope', scope)
        
        if variant_scope == 'class':
            class_id = request.form.get('class_id', type=int)
            if not class_id:
                flash('Выберите класс', 'error')
                return redirect(url_for('variant_create', scope='class'))
            ege_number = None
            variant_id = Variant.create(name, 'class', None, 'class', class_id)
            available_tasks = Task.get_by_class_id(class_id)
            task_ids = request.form.getlist('selected_tasks')
            if task_ids:
                task_ids = [int(tid) for tid in task_ids]
            else:
                task_ids = [t['id'] for t in available_tasks]
            Variant.add_tasks(variant_id, task_ids)
            flash(f'Вариант "{name}" успешно создан', 'success')
            return redirect(url_for('variants_list', scope='class'))
        
        if variant_type == 'thematic':
            ege_number = int(request.form.get('ege_number'))
            tasks_count = int(request.form.get('tasks_count', 10))
            
            # Получаем задачи для этого номера ЕГЭ
            available_tasks = Task.get_by_ege_number(ege_number)
            
            if len(available_tasks) < tasks_count:
                flash(f'Недостаточно задач для номера {ege_number}. Доступно: {len(available_tasks)}', 'error')
                return redirect(url_for('variant_create', scope='ege'))
            
            # Создаём вариант
            variant_id = Variant.create(name, 'thematic', ege_number, 'ege', None)
            
            if generation_mode == 'random':
                # Случайный выбор
                import random
                selected_tasks = random.sample(available_tasks, tasks_count)
                task_ids = [t['id'] for t in selected_tasks]
            else:
                # Ручной выбор - берём выбранные задачи
                task_ids = request.form.getlist('selected_tasks')
                task_ids = [int(tid) for tid in task_ids]
            
            Variant.add_tasks(variant_id, task_ids)
        
        elif variant_type == 'mixed':
            # Смешанный вариант - задачи из разных номеров ЕГЭ
            import random
            variant_id = Variant.create(name, 'mixed', None, 'ege', None)
            task_ids = []
            
            for ege_num in range(1, 28):
                # Проверяем, отмечен ли этот номер
                if request.form.get(f'mixed_ege_{ege_num}'):
                    count = int(request.form.get(f'mixed_count_{ege_num}', 1))
                    available_tasks = Task.get_by_ege_number(ege_num)
                    
                    if len(available_tasks) < count:
                        flash(f'Недостаточно задач для номера {ege_num}. Доступно: {len(available_tasks)}, требуется: {count}', 'warning')
                        count = len(available_tasks)
                    
                    if available_tasks and count > 0:
                        selected = random.sample(available_tasks, count)
                        task_ids.extend([t['id'] for t in selected])
            
            if not task_ids:
                flash('Выберите хотя бы один номер ЕГЭ', 'error')
                return redirect(url_for('variant_create', scope='ege'))
            
            # Перемешиваем задачи
            random.shuffle(task_ids)
            Variant.add_tasks(variant_id, task_ids)
            
        else:  # full - полный вариант ЕГЭ
            variant_id = Variant.create(name, 'full', None, 'ege', None)
            task_ids = []
            
            if generation_mode == 'random':
                import random
                # По одной случайной задаче из каждого номера
                for ege_num in range(1, 28):
                    tasks = Task.get_by_ege_number(ege_num)
                    if tasks:
                        selected = random.choice(tasks)
                        task_ids.append(selected['id'])
                    else:
                        flash(f'Внимание: нет задач для номера {ege_num}', 'warning')
            else:
                # Ручной выбор
                for ege_num in range(1, 28):
                    task_id = request.form.get(f'task_ege_{ege_num}')
                    if task_id:
                        task_ids.append(int(task_id))
            
            Variant.add_tasks(variant_id, task_ids)
        
        flash(f'Вариант "{name}" успешно создан', 'success')
        return redirect(url_for('variants_list', scope='ege'))
    
    # GET - показываем форму
    task_counts = Task.count_by_ege_number()
    all_tasks = {}
    for ege_num in range(1, 28):
        all_tasks[ege_num] = Task.get_by_ege_number(ege_num)
    
    classes = ClassGroup.get_all()
    class_tasks = {}
    for c in classes:
        class_tasks[c['id']] = Task.get_by_class_id(c['id'])
    
    from config import DEFAULT_ANSWER_COUNT
    
    return render_template('teacher/variant_create.html', 
                         task_counts=task_counts,
                         all_tasks=all_tasks,
                         default_answer_count=DEFAULT_ANSWER_COUNT,
                         scope=scope,
                         classes=classes,
                         class_tasks=class_tasks)

@app.route('/variants/<int:variant_id>')
def variant_view(variant_id):
    """Просмотр варианта"""
    variant = Variant.get_by_id(variant_id)
    if not variant:
        flash('Вариант не найден', 'error')
        return redirect(url_for('variants_list'))
    
    tasks = Variant.get_tasks(variant_id)
    return render_template('teacher/variant_view.html', variant=variant, tasks=tasks)

@app.route('/variants/<int:variant_id>/edit', methods=['GET', 'POST'])
def variant_edit(variant_id):
    """Редактирование варианта и состава задач"""
    variant = Variant.get_by_id(variant_id)
    if not variant:
        flash('Вариант не найден', 'error')
        return redirect(url_for('variants_list'))

    current_tasks = Variant.get_tasks(variant_id)

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Название варианта не может быть пустым', 'error')
            return redirect(url_for('variant_edit', variant_id=variant_id))

        remove_ids = request.form.getlist('remove_task_ids')
        add_ids = request.form.getlist('add_task_ids')

        try:
            remove_ids = {int(task_id) for task_id in remove_ids}
            add_ids = [int(task_id) for task_id in add_ids]
        except ValueError:
            flash('Некорректные данные формы', 'error')
            return redirect(url_for('variant_edit', variant_id=variant_id))

        current_ids = [task['id'] for task in current_tasks]
        filtered_ids = [task_id for task_id in current_ids if task_id not in remove_ids]

        # Добавляем новые задачи в конец, избегая дубликатов
        for task_id in add_ids:
            if task_id not in filtered_ids:
                filtered_ids.append(task_id)

        # Добавление новых задач скриншотами
        new_images = [img for img in request.files.getlist('new_images') if img and img.filename]
        new_ege_numbers = request.form.getlist('new_ege_number')
        new_answer_counts = request.form.getlist('new_answer_count')
        new_answers_1 = request.form.getlist('new_answer1')
        new_answers_2 = request.form.getlist('new_answer2')

        created_task_ids = []
        for i, image in enumerate(new_images):
            ege_raw = (new_ege_numbers[i] if i < len(new_ege_numbers) else '').strip()
            answer_count_raw = (new_answer_counts[i] if i < len(new_answer_counts) else '1').strip()
            answer_1 = (new_answers_1[i] if i < len(new_answers_1) else '').strip()
            answer_2 = (new_answers_2[i] if i < len(new_answers_2) else '').strip()

            try:
                ege_number = int(ege_raw)
            except ValueError:
                flash(f'Новая задача #{i + 1}: укажите корректный номер ЕГЭ', 'error')
                continue

            if ege_number < 1 or ege_number > 27:
                flash(f'Новая задача #{i + 1}: номер ЕГЭ должен быть от 1 до 27', 'error')
                continue

            try:
                answer_count = int(answer_count_raw)
            except ValueError:
                answer_count = 1
            answer_count = 2 if answer_count == 2 else 1

            if not answer_1:
                flash(f'Новая задача #{i + 1}: заполните Ответ 1', 'error')
                continue
            if answer_count == 2 and not answer_2:
                flash(f'Новая задача #{i + 1}: заполните Ответ 2', 'error')
                continue

            valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
            if not valid_image:
                flash(f'Новая задача #{i + 1}: {image_error}', 'error')
                continue

            image_filename = generate_unique_filename(image.filename)
            image_path = os.path.join(IMAGES_DIR, image_filename)
            image.save(image_path)

            new_task_id = Task.create(
                ege_number=ege_number,
                image_path=image_filename,
                answer_1=answer_1,
                answer_count=answer_count,
                answer_2=answer_2 if answer_count == 2 else None,
                answer_text=None
            )
            created_task_ids.append(new_task_id)

        filtered_ids.extend(created_task_ids)

        if not filtered_ids:
            flash('В варианте должна остаться хотя бы одна задача', 'error')
            return redirect(url_for('variant_edit', variant_id=variant_id))

        Variant.update(variant_id, name=name)
        Variant.replace_tasks(variant_id, filtered_ids)

        if created_task_ids:
            flash(f'Вариант обновлён, добавлено новых задач: {len(created_task_ids)}', 'success')
        else:
            flash('Вариант успешно обновлён', 'success')
        return redirect(url_for('variant_view', variant_id=variant_id))

    existing_task_ids = {task['id'] for task in current_tasks}
    all_tasks = Task.get_all()
    available_tasks = [task for task in all_tasks if task['id'] not in existing_task_ids]

    return render_template('teacher/variant_edit.html',
                          variant=variant,
                          current_tasks=current_tasks,
                          available_tasks=available_tasks)

@app.route('/variants/<int:variant_id>/delete', methods=['POST'])
def variant_delete(variant_id):
    """Удаление варианта"""
    scope = request.form.get('scope', 'all')
    variant = Variant.get_by_id(variant_id)
    if variant:
        deleted = Variant.delete(variant_id, cascade=True)
        if deleted:
            flash(f'Вариант "{variant["name"]}" удалён вместе с результатами', 'success')
    return redirect(url_for('variants_list', scope=scope))


@app.route('/variants/delete-bulk', methods=['POST'])
def variant_delete_bulk():
    """Массовое удаление вариантов"""
    variant_ids = [int(vid) for vid in request.form.getlist('variant_ids') if str(vid).isdigit()]
    scope = request.form.get('scope', 'all')

    if not variant_ids:
        flash('Отметьте хотя бы один вариант для удаления', 'warning')
        return redirect(url_for('variants_list', scope=scope))

    deleted_count = 0
    for variant_id in variant_ids:
        if Variant.delete(variant_id, cascade=True):
            deleted_count += 1

    flash(f'Удалено вариантов: {deleted_count}', 'success')
    return redirect(url_for('variants_list', scope=scope))


@app.route('/variants/upload', methods=['POST'])
def variant_upload():
    """Загрузка готового варианта с созданием задач в банке"""
    import uuid
    
    name = request.form.get('name')
    if not name:
        flash('Укажите название варианта', 'error')
        return redirect(url_for('variant_create'))
    
    images = request.files.getlist('images')
    if not images or len(images) == 0:
        flash('Загрузите хотя бы одно изображение', 'error')
        return redirect(url_for('variant_create'))
    
    # Фильтруем пустые файлы
    images = [img for img in images if img.filename]
    
    if len(images) == 0:
        flash('Загрузите хотя бы одно изображение', 'error')
        return redirect(url_for('variant_create'))
    
    # Создаём вариант
    variant_id = Variant.create(name, 'uploaded')
    task_ids = []
    
    # Обрабатываем каждое изображение
    for i, image in enumerate(images):
        # Получаем данные формы для этой задачи
        ege_number = request.form.get(f'ege_number_{i}')
        answer_count = request.form.get(f'answer_count_{i}', type=int, default=1)
        if not answer_count or answer_count < 1:
            answer_count = 1
        if answer_count > 20:
            answer_count = 20
        answers = _read_answers_from_form(request.form, answer_count, suffix=f'_{i}')

        if not ege_number or any((v or '').strip() == '' for v in answers):
            continue

        valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
        if not valid_image:
            flash(f'Файл #{i + 1}: {image_error}', 'error')
            continue
        
        ege_number = int(ege_number)
        answer1, answer2, answer_text = _pack_answers_for_task(answers)
        
        # Сохраняем изображение
        ext = os.path.splitext(image.filename)[1] or '.png'
        image_filename = f"{uuid.uuid4().hex}{ext}"
        image_path = os.path.join(DATA_DIR, 'images', image_filename)
        image.save(image_path)
        
        # Обрабатываем прикреплённый файл (если есть)
        attachment_path = None
        attachment_name = None
        attachment = request.files.get(f'attachment_{i}')
        if attachment and attachment.filename:
            valid_attachment, attachment_error = _validate_attachment_file(attachment)
            if not valid_attachment:
                flash(f'Файл данных для задачи #{i + 1}: {attachment_error}', 'error')
                continue
            attach_ext = os.path.splitext(attachment.filename)[1]
            attach_filename = f"{uuid.uuid4().hex}{attach_ext}"
            attach_path = os.path.join(DATA_DIR, 'attachments', attach_filename)
            attachment.save(attach_path)
            attachment_path = attach_filename
            attachment_name = attachment.filename
        
        # Создаём задачу в банке
        task_id = Task.create(
            ege_number=ege_number,
            image_path=image_filename,
            answer_count=answer_count,
            answer_1=answer1,
            answer_2=answer2,
            answer_text=answer_text,
            attachment_path=attachment_path,
            attachment_name=attachment_name
        )
        task_ids.append(task_id)
    
    if not task_ids:
        flash('Не удалось создать ни одной задачи. Проверьте заполнение формы.', 'error')
        Variant.delete(variant_id)
        return redirect(url_for('variant_create'))
    
    # Добавляем задачи в вариант (в порядке загрузки)
    Variant.add_tasks(variant_id, task_ids)
    
    flash(f'Вариант "{name}" создан! Добавлено задач: {len(task_ids)}', 'success')
    return redirect(url_for('variant_view', variant_id=variant_id))

# ==================== ТЕСТИРОВАНИЯ ====================

@app.route('/sessions')
def sessions_list():
    """Список тестирований"""
    status_filter = request.args.get('status', 'all')
    search_query = (request.args.get('q', '') or '').strip().lower()
    selected_session_id = request.args.get('session_id', type=int)

    sessions = TestSession.get_all()
    active_session = TestSession.get_active()
    if active_session and active_session.get('variant_id'):
        active_variant = Variant.get_by_id(active_session['variant_id'])
        active_session['variant_name'] = active_variant['name'] if active_variant else 'Удалён'
    
    # Добавляем информацию о студентах
    filtered_sessions = []
    for s in sessions:
        students = TestSession.get_students(s['id'])
        s['students_count'] = len(students)
        s['finished_count'] = len([st for st in students if st['status'] == 'finished'])
        if s['variant_id']:
            variant = Variant.get_by_id(s['variant_id'])
            s['variant_name'] = variant['name'] if variant else 'Удалён'
        else:
            s['variant_name'] = 'Индивидуальные варианты'

        if status_filter in ('active', 'closed') and s['status'] != status_filter:
            continue
        if search_query and search_query not in s['variant_name'].lower():
            continue
        filtered_sessions.append(s)

    selected_session = None
    selected_students = []
    if filtered_sessions:
        ids = {s['id'] for s in filtered_sessions}
        if not selected_session_id or selected_session_id not in ids:
            selected_session_id = filtered_sessions[0]['id']
        selected_session = next((s for s in filtered_sessions if s['id'] == selected_session_id), None)

    if selected_session:
        selected_students = TestSession.get_students(selected_session['id'])
        session_criteria = {
            'grade_5_min': selected_session.get('grade_5_min'),
            'grade_4_min': selected_session.get('grade_4_min'),
            'grade_3_min': selected_session.get('grade_3_min')
        }
        for st in selected_students:
            if st['status'] == 'finished':
                answers = Student.get_answers(st['id'])
                tasks = Variant.get_tasks(st['variant_id'])
                st['correct_count'] = len([a for a in answers if a['is_correct']])
                st['total_tasks'] = len(tasks)
                st['grade'] = GradeCriteria.calculate_grade(st['correct_count'], st['total_tasks'], session_criteria)
            else:
                st['correct_count'] = 0
                st['total_tasks'] = 0
                st['grade'] = None
    
    return render_template('teacher/sessions.html', 
                         sessions=filtered_sessions,
                         active_session=active_session,
                         selected_session=selected_session,
                         selected_students=selected_students,
                         selected_session_id=selected_session_id,
                         status_filter=status_filter,
                         search_query=search_query)


@app.route('/sessions/delete/<int:session_id>', methods=['POST'])
def session_delete(session_id):
    """Удаление тестирования с результатами"""
    deleted = TestSession.delete_with_results(session_id)
    if deleted:
        flash('Тестирование удалено', 'success')
    else:
        flash('Тестирование не найдено', 'error')

    status = request.form.get('status', 'all')
    q = request.form.get('q', '')
    return redirect(url_for('sessions_list', status=status, q=q))


@app.route('/sessions/delete-bulk', methods=['POST'])
def session_delete_bulk():
    """Массовое удаление тестирований"""
    session_ids = [int(sid) for sid in request.form.getlist('session_ids') if str(sid).isdigit()]
    status = request.form.get('status', 'all')
    q = request.form.get('q', '')

    if not session_ids:
        flash('Отметьте хотя бы одно тестирование для удаления', 'warning')
        return redirect(url_for('sessions_list', status=status, q=q))

    deleted_count = 0
    for session_id in session_ids:
        if TestSession.delete_with_results(session_id):
            deleted_count += 1

    flash(f'Удалено тестирований: {deleted_count}', 'success')
    return redirect(url_for('sessions_list', status=status, q=q))

@app.route('/sessions/new', methods=['GET', 'POST'])
def session_new():
    """Создание нового тестирования"""
    if request.method == 'POST':
        variant_mode = request.form.get('variant_mode')
        time_limit = int(request.form.get('time_limit', 60))
        use_code = request.form.get('use_code') == '1'
        show_answers = request.form.get('show_answers') == '1'
        teacher_finish_only = request.form.get('teacher_finish_only') == '1'
        calculator_enabled = request.form.get('calculator_enabled') == '1'
        python_enabled = request.form.get('python_enabled') == '1'
        
        # Критерии оценки
        total_tasks = int(request.form.get('total_tasks', 8))
        grade_5_min = int(request.form.get('grade_5_min', 7))
        grade_4_min = int(request.form.get('grade_4_min', 5))
        grade_3_min = int(request.form.get('grade_3_min', 3))
        
        # Генерация кода доступа
        access_code = None
        if use_code:
            import random
            import string
            access_code = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        
        if variant_mode == 'single':
            # Один вариант на всех
            variant_id_str = request.form.get('variant_id')
            if not variant_id_str:
                flash('Выберите вариант для тестирования', 'error')
                return redirect(url_for('session_new'))
            
            variant_id = int(variant_id_str)
            session_id = TestSession.create(
                variant_id=variant_id,
                individual_mode=False,
                time_limit=time_limit,
                access_code=access_code,
                show_answers=show_answers,
                teacher_finish_only=teacher_finish_only,
                calculator_enabled=calculator_enabled,
                python_enabled=python_enabled,
                grade_5_min=grade_5_min,
                grade_4_min=grade_4_min,
                grade_3_min=grade_3_min,
                total_tasks=total_tasks
            )
        else:
            # Индивидуальные варианты
            individual_type = request.form.get('individual_type')
            
            if individual_type == 'full':
                session_id = TestSession.create(
                    variant_id=None,
                    individual_mode=True,
                    time_limit=time_limit,
                    access_code=access_code,
                    show_answers=show_answers,
                    teacher_finish_only=teacher_finish_only,
                    calculator_enabled=calculator_enabled,
                    python_enabled=python_enabled,
                    thematic_ege_number=0,  # 0 = полный вариант
                    thematic_tasks_count=27,
                    grade_5_min=grade_5_min,
                    grade_4_min=grade_4_min,
                    grade_3_min=grade_3_min,
                    total_tasks=total_tasks
                )
            else:
                ege_number = int(request.form.get('individual_ege'))
                tasks_count = int(request.form.get('individual_count', 10))
                session_id = TestSession.create(
                    variant_id=None,
                    individual_mode=True,
                    time_limit=time_limit,
                    access_code=access_code,
                    show_answers=show_answers,
                    teacher_finish_only=teacher_finish_only,
                    calculator_enabled=calculator_enabled,
                    python_enabled=python_enabled,
                    thematic_ege_number=ege_number,
                    thematic_tasks_count=tasks_count,
                    grade_5_min=grade_5_min,
                    grade_4_min=grade_4_min,
                    grade_3_min=grade_3_min,
                    total_tasks=total_tasks
                )
        
        flash('Тестирование запущено!', 'success')
        return redirect(url_for('sessions_list'))
    
    # GET - показываем форму
    variants = Variant.get_all()
    for v in variants:
        v['tasks_count'] = len(Variant.get_tasks(v['id']))
    task_counts = Task.count_by_ege_number()
    preselected_variant = request.args.get('variant_id', type=int)
    saved_criteria = GradeCriteria.get_all()
    
    return render_template('teacher/session_new.html',
                         variants=variants,
                         task_counts=task_counts,
                         preselected_variant=preselected_variant,
                         saved_criteria=saved_criteria)

@app.route('/sessions/<int:session_id>/close', methods=['POST'])
def session_close(session_id):
    """Закрыть тестирование"""
    Student.finish_all(session_id)
    TestSession.close(session_id)
    flash('Тестирование завершено', 'success')
    return redirect(url_for('sessions_list'))

@app.route('/sessions/<int:session_id>/pause', methods=['POST'])
def session_pause(session_id):
    """Поставить тестирование на паузу"""
    session = TestSession.get_by_id(session_id)
    if session and session['status'] == 'active':
        TestSession.pause(session_id)
        flash('Тестирование поставлено на паузу', 'success')
    return redirect(url_for('session_monitor', session_id=session_id))

@app.route('/sessions/<int:session_id>/resume', methods=['POST'])
def session_resume(session_id):
    """Продолжить тестирование"""
    session = TestSession.get_by_id(session_id)
    if session and session['status'] == 'active':
        TestSession.resume(session_id)
        flash('Тестирование продолжено', 'success')
    return redirect(url_for('session_monitor', session_id=session_id))

@app.route('/sessions/<int:session_id>/extend', methods=['POST'])
def session_extend(session_id):
    """Продлить тестирование"""
    session = TestSession.get_by_id(session_id)
    if session and session['status'] == 'active':
        minutes = request.form.get('minutes', '5')
        try:
            minutes_int = int(minutes)
        except Exception:
            minutes_int = 5
        if minutes_int > 0:
            TestSession.extend_time(session_id, minutes_int * 60)
            flash(f'Время продлено на {minutes_int} мин', 'success')
    return redirect(url_for('session_monitor', session_id=session_id))

@app.route('/sessions/<int:session_id>/monitor')
def session_monitor(session_id):
    """Мониторинг тестирования"""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('sessions_list'))
    
    students = TestSession.get_students(session_id)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    online_count = 0
    for s in students:
        last_seen = _parse_dt(s.get('last_seen_at'))
        s['last_seen_at'] = last_seen.isoformat() if last_seen else None
        s['is_online'] = bool(last_seen and (now - last_seen).total_seconds() <= 25)
        if s['is_online']:
            online_count += 1
        s['file_uploads_count'] = Answer.count_uploads_for_student(s['id'])
    
    return render_template('teacher/session_monitor.html',
                         session=session,
                         students=students,
                         online_count=online_count)


# ==================== ИНТЕРФЕЙС УЧЕНИКА ====================

@app.route('/test')
def student_login():
    """Страница входа ученика"""
    active_session = TestSession.get_active()
    if not active_session:
        return render_template('student/no_test.html')

    from flask import session
    student_id = session.get('student_id')
    if student_id:
        student = Student.get_by_id(student_id)
        if student and student['status'] == 'in_progress':
            test_session = TestSession.get_by_id(student['session_id'])
            if test_session and test_session['status'] == 'active':
                return redirect(url_for('student_test'))
            if test_session and test_session['status'] == 'closed':
                return redirect(url_for('student_result'))
        session.clear()
    
    need_code = active_session['access_code'] is not None
    app_mode = request.args.get('app') == '1'
    autocomplete_section = secrets.token_hex(8)
    return render_template('student/login.html',
                          need_code=need_code,
                          app_mode=app_mode,
                          autocomplete_section=autocomplete_section)

@app.route('/test/start', methods=['POST'])
def student_start():
    """Начало теста учеником"""
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    code = request.form.get('code', '').strip().upper()
    app_mode = request.form.get('app_mode') == '1'
    
    if not first_name or not last_name:
        flash('Введите имя и фамилию', 'error')
        return redirect(url_for('student_login'))
    
    active_session = TestSession.get_active()
    if not active_session:
        flash('Нет активного тестирования', 'error')
        return redirect(url_for('student_login'))
    
    # Проверка кода
    if active_session['access_code'] and active_session['access_code'] != code:
        flash('Неверный код доступа', 'error')
        return redirect(url_for('student_login'))
    
    # Проверка на повторный вход
    existing = Student.get_by_session_and_name(active_session['id'], first_name, last_name)
    if existing:
        if existing['status'] == 'in_progress':
            from flask import session
            session['student_id'] = existing['id']
            session['start_time'] = (existing.get('started_at') or datetime.now().isoformat())
            session['time_limit'] = active_session['time_limit']
            session['app_mode'] = app_mode
            Student.touch(existing['id'])
            return redirect(url_for('student_test'))
        flash('Вы уже завершили этот тест', 'error')
        return redirect(url_for('student_login'))
    
    # Определяем вариант для ученика
    if active_session['individual_mode']:
        # Генерируем индивидуальный вариант
        import random
        from models import get_db
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Получаем настройки сессии
        cursor.execute("SELECT value FROM settings WHERE key = ?", 
                      (f"session_{active_session['id']}_ege",))
        ege_row = cursor.fetchone()
        cursor.execute("SELECT value FROM settings WHERE key = ?", 
                      (f"session_{active_session['id']}_count",))
        count_row = cursor.fetchone()
        conn.close()
        
        ege_number = int(ege_row['value']) if ege_row else 0
        tasks_count = int(count_row['value']) if count_row else 10
        
        # Создаём временный вариант
        variant_name = f"Индивидуальный_{first_name}_{last_name}_{active_session['id']}"
        
        if ege_number == 0:
            # Полный вариант
            variant_id = Variant.create(variant_name, 'full')
            task_ids = []
            for num in range(1, 28):
                tasks = Task.get_by_ege_number(num)
                if tasks:
                    task_ids.append(random.choice(tasks)['id'])
        else:
            # Тематический вариант
            variant_id = Variant.create(variant_name, 'thematic', ege_number)
            tasks = Task.get_by_ege_number(ege_number)
            selected = random.sample(tasks, min(tasks_count, len(tasks)))
            task_ids = [t['id'] for t in selected]
        
        Variant.add_tasks(variant_id, task_ids)
    else:
        variant_id = active_session['variant_id']
    
    # Создаём запись ученика
    student_id = Student.create(active_session['id'], first_name, last_name, variant_id)
    Student.touch(student_id)
    
    # Сохраняем в сессию
    from flask import session
    session['student_id'] = student_id
    session['start_time'] = datetime.now().isoformat()
    session['time_limit'] = active_session['time_limit']
    session['app_mode'] = app_mode
    
    return redirect(url_for('student_test'))

@app.route('/test/exam')
def student_test():
    """Страница прохождения теста"""
    from flask import session
    
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('student_login'))
    
    student = Student.get_by_id(student_id)
    if not student or student['status'] == 'finished':
        session.clear()
        return redirect(url_for('student_login'))

    test_session = TestSession.get_by_id(student['session_id'])
    if test_session and test_session['status'] == 'closed':
        Student.finish(student_id)
        return redirect(url_for('student_result'))
    
    # Получаем задачи варианта
    tasks = Variant.get_tasks(student['variant_id'])
    
    # Получаем уже сохранённые ответы
    answers = {}
    for task in tasks:
        ans = Answer.get_for_student_task(student_id, task['id'])
        if ans:
            answers[task['id']] = ans
    
    # Вычисляем оставшееся время по данным БД (устойчиво к перезапуску браузера)
    remaining = _calc_remaining_seconds(student, test_session)
    
    current_task = request.args.get('task', 0, type=int)
    
    return render_template('student/test.html',
                          student=student,
                          tasks=tasks,
                          answers=answers,
                          remaining=int(remaining),
                          current_task=current_task,
                          teacher_finish_only=bool(test_session and test_session.get('teacher_finish_only')),
                          app_mode=bool(session.get('app_mode')),
                          paused=bool(test_session and test_session.get('paused')),
                          calculator_enabled=bool(test_session and test_session.get('calculator_enabled')),
                          python_enabled=bool(test_session and test_session.get('python_enabled')))

@app.route('/test/save', methods=['POST'])
def student_save_answer():
    """Сохранение ответа ученика"""
    from flask import session
    
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Не авторизован'}), 401
    
    data = request.get_json()
    task_id = data.get('task_id')
    answer_1 = data.get('answer_1')
    answer_2 = data.get('answer_2')
    answer_text = data.get('answer_text')

    # Разрешаем ответы не только числами: сохраняем как строки.
    # Сравнение/нормализация делаются в Answer.save().
    answer_1 = (str(answer_1).strip() if answer_1 not in [None, ''] else None)
    answer_2 = (str(answer_2).strip() if answer_2 not in [None, ''] else None)
    answer_text = (str(answer_text) if answer_text not in [None] else None)

    Answer.save(student_id, task_id, answer_1, answer_2, answer_text=answer_text)
    Student.touch(student_id)
    
    return jsonify({'success': True})

@app.route('/test/finish', methods=['POST'])
def student_finish():
    """Завершение теста"""
    from flask import session
    
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('student_login'))
    
    student = Student.get_by_id(student_id)
    if not student:
        return redirect(url_for('student_login'))

    test_session = TestSession.get_by_id(student['session_id'])
    if test_session and test_session.get('teacher_finish_only') and test_session['status'] != 'closed':
        return redirect(url_for('student_test'))

    Student.finish(student_id)

    return redirect(url_for('student_result'))

@app.route('/test/ping', methods=['POST'])
def student_ping():
    """Проверка состояния тестирования для ученика"""
    from flask import session

    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'active': False}), 401

    student = Student.get_by_id(student_id)
    if not student:
        return jsonify({'active': False}), 404

    test_session = TestSession.get_by_id(student['session_id'])
    if not test_session:
        return jsonify({'active': False}), 404

    Student.touch(student_id)
    remaining = _calc_remaining_seconds(student, test_session)

    if test_session['status'] == 'closed':
        if student['status'] != 'finished':
            Student.finish(student_id)
        return jsonify({'active': False, 'finished': True, 'redirect': url_for('student_result')})

    return jsonify({'active': True, 'paused': bool(test_session.get('paused')), 'remaining_seconds': remaining})

@app.route('/test/run-python', methods=['POST'])
def student_run_python():
    """Запуск Python-кода на компьютере учителя.
    
    Код выполняется реальным Python с доступом к файлу задачи.
    Ошибки возвращаются в точном формате IDLE (traceback as-is).
    """
    import ast
    import json
    import subprocess
    import tempfile
    import shutil
    import textwrap
    from flask import session as flask_session

    student_id = flask_session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Неавторизован'}), 401

    data = request.get_json(silent=True) or {}
    code = data.get('code', '')
    # attachment_path — хэш-имя файла в data/attachments (напр. "abc123.txt")
    attachment_path = data.get('attachment_path') or ''
    # attachment_name — оригинальное имя файла (напр. "input.txt"), которое пишет ученик в open()
    attachment_name = data.get('attachment_name') or ''
    safe_attachment_name = os.path.basename(attachment_name)

    if not code or not code.strip():
        return jsonify({'error': 'Пустой код'}), 400
    if len(code) > 50000:
        return jsonify({'error': 'Код слишком большой (максимум 50 000 символов)'}), 400

    # Жесткая валидация AST: опасные импорты/вызовы/атрибуты блокируются до запуска
    allowed_import_roots = {
        'math', 'random', 'itertools', 'functools', 'collections', 'heapq',
        'bisect', 'string', 're', 'statistics', 'fractions', 'decimal', 'datetime'
    }
    blocked_call_names = {
        '__import__', 'eval', 'exec', 'compile', 'breakpoint', 'help'
    }
    blocked_attr_names = {
        'system', 'popen', 'Popen', 'check_output', 'check_call',
        'remove', 'unlink', 'rmdir', 'removedirs', 'rename', 'replace', 'chdir',
        'listdir', 'scandir', 'walk', 'mkdir', 'makedirs', 'rmtree',
        'startfile', 'kill', 'spawn', 'fork', 'forkpty', 'execv', 'execve',
        'execl', 'execlp', 'execvp', 'execvpe'
    }

    try:
        tree = ast.parse(code, filename='solution.py', mode='exec')
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split('.')[0]
                    if root not in allowed_import_roots:
                        return jsonify({'error': f'Запрещен импорт модуля: {root}'}), 400
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or '').split('.')[0]
                if node.level and node.level > 0:
                    return jsonify({'error': 'Относительные импорты запрещены'}), 400
                if root not in allowed_import_roots:
                    return jsonify({'error': f'Запрещен импорт модуля: {root}'}), 400
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in blocked_call_names:
                    return jsonify({'error': f'Запрещен вызов: {node.func.id}()'}), 400
            elif isinstance(node, ast.Attribute) and node.attr in blocked_attr_names:
                return jsonify({'error': f'Запрещено использовать опасный атрибут: {node.attr}'}), 400
    except SyntaxError:
        # Синтаксические ошибки отдаст сам Python в формате traceback
        pass

    # Создаём временную рабочую директорию
    # В ней будет лежать код ученика + (если есть) файл задачи с оригинальным именем
    work_dir = tempfile.mkdtemp(prefix='py_run_')
    temp_script = os.path.join(work_dir, 'solution.py')
    runner_script = os.path.join(work_dir, 'runner.py')
    try:
        # Записываем код ученика
        with open(temp_script, 'w', encoding='utf-8') as f:
            f.write(code)

        # Копируем файл задачи рядом с кодом под оригинальным именем
        if attachment_path and safe_attachment_name:
            src = os.path.join(ATTACHMENTS_DIR, attachment_path)
            if os.path.isfile(src):
                dst = os.path.join(work_dir, safe_attachment_name)
                shutil.copy2(src, dst)

        # Изолированный раннер: ограниченные builtins + безопасный import/open
        runner_code = textwrap.dedent(f"""
            import builtins
            import os

            SAFE_MODULES = {json.dumps(sorted(list(allowed_import_roots)))}
            ALLOWED_FILES = {json.dumps([safe_attachment_name] if safe_attachment_name else [])}

            _real_import = builtins.__import__
            _real_open = builtins.open

            def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
                root = (name or '').split('.')[0]
                if root not in SAFE_MODULES:
                    raise ImportError(f"Import '{{root}}' is blocked")
                return _real_import(name, globals, locals, fromlist, level)

            def _safe_open(file, mode='r', *args, **kwargs):
                if not isinstance(file, (str, bytes, os.PathLike)):
                    raise PermissionError('open() supports only filesystem path')
                path = os.fspath(file)
                if isinstance(path, bytes):
                    path = path.decode('utf-8', errors='ignore')

                if os.path.isabs(path):
                    raise PermissionError('Absolute paths are blocked')

                normalized = os.path.normpath(path)
                if normalized == '..' or normalized.startswith('..' + os.sep):
                    raise PermissionError('Path traversal is blocked')

                base = os.path.basename(normalized)
                if base != normalized:
                    raise PermissionError('Only files in current directory are allowed')

                if not ALLOWED_FILES or base not in ALLOWED_FILES:
                    raise PermissionError('Access only to task attachment is allowed')

                mode_str = str(mode or 'r')
                if any(ch in mode_str for ch in ('w', 'a', 'x', '+')):
                    raise PermissionError('Write mode is blocked')

                return _real_open(base, mode, *args, **kwargs)

            _safe_builtins = {{
                'abs': builtins.abs,
                'all': builtins.all,
                'any': builtins.any,
                'ascii': builtins.ascii,
                'bin': builtins.bin,
                'bool': builtins.bool,
                'bytearray': builtins.bytearray,
                'bytes': builtins.bytes,
                'callable': builtins.callable,
                'chr': builtins.chr,
                'complex': builtins.complex,
                'dict': builtins.dict,
                'divmod': builtins.divmod,
                'enumerate': builtins.enumerate,
                'filter': builtins.filter,
                'float': builtins.float,
                'format': builtins.format,
                'frozenset': builtins.frozenset,
                'hash': builtins.hash,
                'hex': builtins.hex,
                'int': builtins.int,
                'isinstance': builtins.isinstance,
                'issubclass': builtins.issubclass,
                'iter': builtins.iter,
                'len': builtins.len,
                'list': builtins.list,
                'map': builtins.map,
                'max': builtins.max,
                'min': builtins.min,
                'next': builtins.next,
                'oct': builtins.oct,
                'ord': builtins.ord,
                'pow': builtins.pow,
                'print': builtins.print,
                'range': builtins.range,
                'repr': builtins.repr,
                'reversed': builtins.reversed,
                'round': builtins.round,
                'set': builtins.set,
                'slice': builtins.slice,
                'sorted': builtins.sorted,
                'str': builtins.str,
                'sum': builtins.sum,
                'tuple': builtins.tuple,
                'type': builtins.type,
                'zip': builtins.zip,
                'open': _safe_open,
                '__import__': _safe_import,
                'Exception': builtins.Exception,
                'ValueError': builtins.ValueError,
                'TypeError': builtins.TypeError,
                'NameError': builtins.NameError,
                'IndexError': builtins.IndexError,
                'KeyError': builtins.KeyError,
                'ZeroDivisionError': builtins.ZeroDivisionError,
                'OverflowError': builtins.OverflowError,
                'RuntimeError': builtins.RuntimeError,
                'StopIteration': builtins.StopIteration,
                'ArithmeticError': builtins.ArithmeticError,
                'AssertionError': builtins.AssertionError,
            }}

            _globals = {{'__name__': '__main__', '__builtins__': _safe_builtins}}

            with _real_open('solution.py', 'r', encoding='utf-8', errors='replace') as _f:
                _code = _f.read()
            _compiled = compile(_code, 'solution.py', 'exec')
            exec(_compiled, _globals, None)
        """)

        with open(runner_script, 'w', encoding='utf-8') as f:
            f.write(runner_code)

        # Запускаем Python в изолированном режиме:
        # -I: ignore user environment/site
        # -S: do not import site
        # cwd = work_dir, поэтому open('input.txt') найдёт файл задачи
        result = subprocess.run(
            ['python', '-I', '-S', 'runner.py'],
            capture_output=True,
            text=True,
            timeout=10,
            encoding='utf-8',
            errors='replace',
            cwd=work_dir,
            stdin=subprocess.DEVNULL,
            env=None
        )

        stdout = result.stdout
        stderr = result.stderr

        # Очищаем traceback: заменяем путь к временному файлу на "solution.py"
        # чтобы ученик видел "File solution.py, line N" — как в IDLE
        if stderr:
            stderr = stderr.replace(temp_script, 'solution.py')
            # Убираем абсолютные пути рабочей директории
            stderr = stderr.replace(work_dir + os.sep, '')
            stderr = stderr.replace(work_dir + '/', '')

        return jsonify({
            'success': result.returncode == 0,
            'stdout': stdout[:10000],
            'stderr': stderr[:10000],
        })

    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Превышен лимит времени выполнения (10 сек).\nПроверьте бесконечные циклы.'}), 400
    except Exception as e:
        return jsonify({'error': f'Ошибка сервера: {str(e)}'}), 500
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

@app.route('/test/result')
def student_result():
    """Результат ученика"""
    from flask import session as flask_session
    
    student_id = flask_session.get('student_id')
    if not student_id:
        return redirect(url_for('student_login'))
    
    student = Student.get_by_id(student_id)
    if not student:
        flask_session.clear()
        return redirect(url_for('student_login'))

    test_session = TestSession.get_by_id(student['session_id'])

    if test_session and test_session.get('teacher_finish_only') and test_session['status'] != 'closed':
        return redirect(url_for('student_test'))

    if student['status'] != 'finished':
        Student.finish(student_id)
    
    # Получаем все ответы
    answers = Student.get_answers(student_id)
    tasks = Variant.get_tasks(student['variant_id'])
    
    # Создаём словарь ответов по task_id
    answers_dict = {a['task_id']: a for a in answers}
    
    correct_count = len([a for a in answers if a['is_correct']])
    total = len(tasks)
    
    # Рассчитываем оценку с учётом критериев сессии
    session_criteria = {
        'grade_5_min': test_session.get('grade_5_min') if test_session else None,
        'grade_4_min': test_session.get('grade_4_min') if test_session else None,
        'grade_3_min': test_session.get('grade_3_min') if test_session else None,
    }
    grade = GradeCriteria.calculate_grade(correct_count, total, session_criteria)
    
    # Очищаем сессию
    flask_session.clear()
    
    return render_template('student/result.html',
                          student=student,
                          answers=answers,
                          answers_dict=answers_dict,
                          tasks=tasks,
                          correct_count=correct_count,
                          total=total,
                          grade=grade,
                         show_answers=(
                             True if (test_session and test_session.get('teacher_finish_only') and test_session['status'] == 'closed')
                             else (test_session['show_answers'] if test_session else False)
                         ))

# ==================== РЕЗУЛЬТАТЫ ====================

@app.route('/results')
def results_list():
    """Список результатов по тестированиям"""
    from datetime import datetime, timedelta

    filter_period = request.args.get('period', 'all')
    search_query = (request.args.get('q', '') or '').strip().lower()
    selected_session_id = request.args.get('session_id', type=int)

    sessions = TestSession.get_all()

    now = datetime.now()
    if filter_period == 'week':
        cutoff = now - timedelta(days=7)
    elif filter_period == 'month':
        cutoff = now - timedelta(days=30)
    elif filter_period == 'quarter':
        cutoff = now - timedelta(days=90)
    elif filter_period == 'year':
        cutoff = now - timedelta(days=365)
    else:
        cutoff = None

    filtered_sessions = []
    for s in sessions:
        if s['variant_id']:
            variant = Variant.get_by_id(s['variant_id'])
            s['variant_name'] = variant['name'] if variant else 'Удалён'
        else:
            s['variant_name'] = 'Индивидуальные варианты'

        if cutoff:
            try:
                created = datetime.fromisoformat((s.get('created_at') or '').replace(' ', 'T'))
                if created < cutoff:
                    continue
            except Exception:
                pass

        if search_query and search_query not in s['variant_name'].lower():
            continue

        students = TestSession.get_students(s['id'])
        s['students_count'] = len(students)
        s['finished_count'] = len([st for st in students if st['status'] == 'finished'])
        filtered_sessions.append(s)

    selected_session = None
    selected_students = []

    if filtered_sessions:
        session_ids = {s['id'] for s in filtered_sessions}
        if not selected_session_id or selected_session_id not in session_ids:
            selected_session_id = filtered_sessions[0]['id']

        selected_session = next((s for s in filtered_sessions if s['id'] == selected_session_id), None)

    if selected_session:
        selected_students = TestSession.get_students(selected_session['id'])
        session_criteria = {
            'grade_5_min': selected_session.get('grade_5_min'),
            'grade_4_min': selected_session.get('grade_4_min'),
            'grade_3_min': selected_session.get('grade_3_min')
        }

        total_correct_all = 0
        finished_for_avg = 0
        for st in selected_students:
            if st['status'] == 'finished':
                answers = Student.get_answers(st['id'])
                tasks = Variant.get_tasks(st['variant_id'])
                st['correct_count'] = len([a for a in answers if a['is_correct']])
                st['total_tasks'] = len(tasks)
                st['grade'] = GradeCriteria.calculate_grade(st['correct_count'], st['total_tasks'], session_criteria)
                total_correct_all += st['correct_count']
                finished_for_avg += 1
            else:
                st['correct_count'] = 0
                st['total_tasks'] = 0
                st['grade'] = None

        selected_session['avg_score'] = round(total_correct_all / finished_for_avg, 1) if finished_for_avg > 0 else 0

    return render_template(
        'teacher/results.html',
        sessions=filtered_sessions,
        selected_session=selected_session,
        selected_students=selected_students,
        selected_session_id=selected_session_id,
        total_sessions=len(filtered_sessions),
        filter_period=filter_period,
        search_query=search_query,
    )

@app.route('/results/session/<int:session_id>')
def result_session(session_id):
    """Результаты по конкретному тестированию"""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('results_list'))
    
    students = TestSession.get_students(session_id)
    
    # Критерии оценки для этой сессии
    session_criteria = {
        'grade_5_min': session.get('grade_5_min'),
        'grade_4_min': session.get('grade_4_min'),
        'grade_3_min': session.get('grade_3_min')
    }
    
    # Добавляем результаты каждого ученика
    for st in students:
        answers = Student.get_answers(st['id'])
        tasks = Variant.get_tasks(st['variant_id'])
        st['correct'] = len([a for a in answers if a['is_correct']])
        st['total'] = len(tasks)
        st['grade'] = GradeCriteria.calculate_grade(st['correct'], st['total'], session_criteria)
    
    return render_template('teacher/result_session.html', session=session, students=students)

@app.route('/results/student/<int:student_id>')
def result_student(student_id):
    """Детальный результат ученика"""
    student = Student.get_by_id(student_id)
    if not student:
        flash('Ученик не найден', 'error')
        return redirect(url_for('results_list'))
    
    test_session = TestSession.get_by_id(student['session_id'])
    answers = Student.get_answers(student_id)
    tasks = Variant.get_tasks(student['variant_id'])
    
    # Создаём словарь ответов по task_id
    answers_dict = {a['task_id']: a for a in answers}
    
    correct_count = len([a for a in answers if a['is_correct']])
    total = len(tasks)
    
    # Рассчитываем оценку с учётом критериев сессии
    session_criteria = {
        'grade_5_min': test_session.get('grade_5_min') if test_session else None,
        'grade_4_min': test_session.get('grade_4_min') if test_session else None,
        'grade_3_min': test_session.get('grade_3_min') if test_session else None,
    }
    grade = GradeCriteria.calculate_grade(correct_count, total, session_criteria)
    
    return render_template('teacher/result_student.html',
                         student=student,
                         answers=answers,
                         answers_dict=answers_dict,
                         tasks=tasks,
                         correct_count=correct_count,
                         total=total,
                         grade=grade)

@app.route('/results/export/<int:session_id>')
def result_export(session_id):
    """Экспорт результатов в CSV"""
    import csv
    from io import StringIO
    from flask import Response
    
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('results_list'))
    
    students = TestSession.get_students(session_id)
    
    # Создаём CSV
    output = StringIO()
    writer = csv.writer(output, delimiter=';')
    
    # Заголовок
    writer.writerow(['Фамилия', 'Имя', 'Начало', 'Завершение', 'Правильных', 'Всего', 'Оценка'])
    
    for st in students:
        answers = Student.get_answers(st['id'])
        tasks = Variant.get_tasks(st['variant_id'])
        correct = len([a for a in answers if a['is_correct']])
        total = len(tasks)
        grade = GradeCriteria.calculate_grade(correct, total)
        
        writer.writerow([
            st['last_name'],
            st['first_name'],
            to_local_dt(st['started_at']) if st['started_at'] else '',
            to_local_dt(st['finished_at']) if st['finished_at'] else '',
            correct,
            total,
            grade
        ])
    
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=results_{session_id}.csv'}
    )


@app.route('/results/delete/<int:session_id>', methods=['POST'])
def result_delete(session_id):
    """Удаление результатов тестирования"""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('results_list'))

    deleted = TestSession.delete_with_results(session_id)
    if deleted:
        flash('Результаты тестирования удалены', 'success')
    else:
        flash('Не удалось удалить результаты', 'error')

    period = request.form.get('period', 'all')
    q = request.form.get('q', '')
    return redirect(url_for('results_list', period=period, q=q))


@app.route('/results/delete-bulk', methods=['POST'])
def result_delete_bulk():
    """Массовое удаление результатов тестирований"""
    session_ids = [int(sid) for sid in request.form.getlist('session_ids') if str(sid).isdigit()]
    period = request.form.get('period', 'all')
    q = request.form.get('q', '')

    if not session_ids:
        flash('Отметьте хотя бы одно тестирование для удаления', 'warning')
        return redirect(url_for('results_list', period=period, q=q))

    deleted_count = 0
    for session_id in session_ids:
        if TestSession.delete_with_results(session_id):
            deleted_count += 1

    if deleted_count:
        flash(f'Удалено тестирований: {deleted_count}', 'success')
    else:
        flash('Не удалось удалить выбранные тестирования', 'error')

    return redirect(url_for('results_list', period=period, q=q))


# ==================== НАСТРОЙКИ ====================

@app.route('/settings')
def settings():
    """Страница настроек"""
    criteria = GradeCriteria.get_all()
    return render_template('teacher/settings.html', criteria=criteria)

@app.route('/settings/criteria', methods=['POST'])
def update_criteria():
    """Обновление критериев оценки"""
    name = request.form.get('name')
    total_tasks = int(request.form.get('total_tasks'))
    grade_5_min = int(request.form.get('grade_5_min'))
    grade_4_min = int(request.form.get('grade_4_min'))
    grade_3_min = int(request.form.get('grade_3_min'))
    
    GradeCriteria.create_or_update(name, total_tasks, grade_5_min, grade_4_min, grade_3_min)
    flash('Критерии оценки сохранены', 'success')
    return redirect(url_for('settings'))


# ==================== ИМПОРТ/ЭКСПОРТ ====================

@app.route('/tasks/export')
def tasks_export():
    """Экспорт банка задач в ZIP"""
    import zipfile
    import json
    import io
    
    # Получаем все задачи
    all_tasks = []
    for ege_num in range(1, 28):
        tasks = Task.get_by_ege_number(ege_num)
        all_tasks.extend(tasks)
    
    if not all_tasks:
        flash('Нет задач для экспорта', 'error')
        return redirect(url_for('tasks_list'))
    
    # Создаём ZIP в памяти
    zip_buffer = io.BytesIO()
    
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        # Манифест с метаданными
        manifest = {
            'version': '1.0',
            'exported_at': datetime.now().isoformat(),
            'tasks': []
        }
        
        for task in all_tasks:
            task_data = {
                'ege_number': task['ege_number'],
                'answer_count': task['answer_count'],
                'answer_1': task['answer_1'],
                'answer_2': task['answer_2'],
                'answer_text': task.get('answer_text'),
                'image_filename': task['image_path'],
                'attachment_filename': task.get('attachment_name'),
                'attachment_path': task.get('attachment_path')
            }
            manifest['tasks'].append(task_data)
            
            # Добавляем изображение
            image_path = os.path.join(DATA_DIR, 'images', task['image_path'])
            if os.path.exists(image_path):
                zip_file.write(image_path, f"images/{task['image_path']}")
            
            # Добавляем прикреплённый файл
            if task.get('attachment_path'):
                attach_path = os.path.join(DATA_DIR, 'attachments', task['attachment_path'])
                if os.path.exists(attach_path):
                    zip_file.write(attach_path, f"attachments/{task['attachment_path']}")
        
        # Записываем манифест
        zip_file.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
    
    zip_buffer.seek(0)
    
    filename = f"bank_zadach_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(
        zip_buffer,
        mimetype='application/zip',
        as_attachment=True,
        download_name=filename
    )


@app.route('/tasks/import', methods=['GET', 'POST'])
def tasks_import():
    """Импорт банка задач из ZIP"""
    import zipfile
    import json
    
    if request.method == 'POST':
        if 'file' not in request.files:
            flash('Файл не выбран', 'error')
            return redirect(url_for('tasks_import'))
        
        file = request.files['file']
        if file.filename == '':
            flash('Файл не выбран', 'error')
            return redirect(url_for('tasks_import'))

        valid_zip, zip_error = _validate_upload(file, MAX_IMPORT_ZIP_SIZE, {'zip'}, 'zip')
        if not valid_zip:
            flash(zip_error or 'Файл должен быть ZIP-архивом', 'error')
            return redirect(url_for('tasks_import'))
        
        try:
            with zipfile.ZipFile(file, 'r') as zip_file:
                infos = zip_file.infolist()
                if len(infos) > 2000:
                    flash('Слишком много файлов в архиве', 'error')
                    return redirect(url_for('tasks_import'))

                total_uncompressed = sum(i.file_size for i in infos)
                if total_uncompressed > 200 * 1024 * 1024:
                    flash('Архив слишком большой после распаковки', 'error')
                    return redirect(url_for('tasks_import'))

                # Читаем манифест
                if 'manifest.json' not in zip_file.namelist():
                    flash('Неверный формат архива (нет manifest.json)', 'error')
                    return redirect(url_for('tasks_import'))
                
                manifest = json.loads(zip_file.read('manifest.json'))
                if not isinstance(manifest, dict) or not isinstance(manifest.get('tasks'), list):
                    flash('Неверный формат manifest.json', 'error')
                    return redirect(url_for('tasks_import'))
                
                imported_count = 0
                skipped_count = 0
                
                for task_data in manifest['tasks']:
                    if not isinstance(task_data, dict):
                        skipped_count += 1
                        continue

                    # Извлекаем изображение
                    image_filename = task_data['image_filename']
                    if not _is_safe_stored_name(image_filename) or not allowed_file(image_filename, ALLOWED_IMAGE_EXTENSIONS):
                        skipped_count += 1
                        continue
                    image_zip_path = f"images/{image_filename}"
                    
                    if image_zip_path not in zip_file.namelist():
                        skipped_count += 1
                        continue
                    
                    # Сохраняем изображение
                    image_content = zip_file.read(image_zip_path)
                    image_save_path = os.path.join(DATA_DIR, 'images', image_filename)
                    
                    # Если файл уже существует, генерируем новое имя
                    if os.path.exists(image_save_path):
                        import uuid
                        new_name = f"{uuid.uuid4().hex}.png"
                        image_save_path = os.path.join(DATA_DIR, 'images', new_name)
                        image_filename = new_name
                    
                    with open(image_save_path, 'wb') as f:
                        f.write(image_content)
                    
                    # Обрабатываем прикреплённый файл
                    attachment_path = None
                    attachment_name = task_data.get('attachment_filename')
                    if task_data.get('attachment_path'):
                        source_attachment_path = task_data['attachment_path']
                        if not _is_safe_stored_name(source_attachment_path) or not allowed_file(source_attachment_path, ALLOWED_ATTACHMENT_EXTENSIONS):
                            source_attachment_path = None
                        attach_zip_path = f"attachments/{source_attachment_path}" if source_attachment_path else None
                        if attach_zip_path in zip_file.namelist():
                            attach_content = zip_file.read(attach_zip_path)
                            attach_save_path = os.path.join(DATA_DIR, 'attachments', source_attachment_path)
                            
                            if os.path.exists(attach_save_path):
                                import uuid
                                ext = os.path.splitext(source_attachment_path)[1]
                                new_name = f"{uuid.uuid4().hex}{ext}"
                                attach_save_path = os.path.join(DATA_DIR, 'attachments', new_name)
                                attachment_path = new_name
                            else:
                                attachment_path = source_attachment_path
                            
                            with open(attach_save_path, 'wb') as f:
                                f.write(attach_content)
                    
                    # Создаём задачу в БД
                    Task.create(
                        ege_number=task_data['ege_number'],
                        image_path=image_filename,
                        answer_count=task_data['answer_count'],
                        answer_1=task_data['answer_1'],
                        answer_2=task_data.get('answer_2'),
                        answer_text=task_data.get('answer_text'),
                        attachment_path=attachment_path,
                        attachment_name=attachment_name
                    )
                    imported_count += 1
                
                flash(f'Импортировано задач: {imported_count}. Пропущено: {skipped_count}', 'success')
                return redirect(url_for('tasks_list'))
                
        except Exception as e:
            flash(f'Ошибка при импорте: {str(e)}', 'error')
            return redirect(url_for('tasks_import'))
    
    return render_template('teacher/tasks_import.html')


# ==================== ЗАПУСК ====================

if __name__ == '__main__':
    print('=' * 50)
    print('Система тестирования ЕГЭ по информатике')
    print('=' * 50)
    print(f'Сервер запущен: http://{get_local_ip()}:{PORT}')
    print(f'Локальный адрес: http://127.0.0.1:{PORT}')
    print('=' * 50)
    print('Для остановки нажмите Ctrl+C')
    print()
    
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
