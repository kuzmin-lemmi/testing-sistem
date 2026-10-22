"""Ученик: вход, решение теста, сохранение ответов, запуск Python, итог."""
import json
import os
import secrets
import socket
import uuid
from datetime import datetime, timezone

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.utils import secure_filename

from activity import log_event
from blueprints.common import (
    _calc_remaining_seconds,
    _clear_student_session,
    _get_session_variant_options,
    _parse_dt,
    _tasks_for_student,
)
from config import ALLOWED_STUDENT_UPLOAD_EXTENSIONS, ATTACHMENTS_DIR, MAX_STUDENT_UPLOAD_SIZE, STUDENT_UPLOADS_DIR
from models import Answer, GradeCriteria, Student, Task, TestSession, Variant

bp = Blueprint('student', __name__)


@bp.route('/test/alive')
def student_alive():
    """Ярлык на компьютере ученика проверяет, что система запущена (с любого компьютера сети)."""
    return 'ok', 200, {'Content-Type': 'text/plain; charset=utf-8'}


@bp.route('/test/hello')
def student_hello():
    """Программа ученика ищет систему в сети: это именно система тестирования, и на каком она компьютере."""
    return jsonify({'app': 'testing', 'computer': socket.gethostname()})


@bp.route('/test/state')
def student_state():
    """Есть ли запущенное тестирование: страницы ученика следят за этим и обновляются сами."""
    active = TestSession.get_active()
    return jsonify({'active': bool(active), 'session_id': active['id'] if active else None})


# ==================== ЗАГРУЗКА ФАЙЛОВ УЧЕНИКОВ ====================

@bp.route('/test/upload-answer-file', methods=['POST'])
def student_upload_answer_file():
    """Ученик загружает файл (.ods/.odt) как ответ на задание типа file_upload."""
    from flask import session as flask_session
    student_id = flask_session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Не авторизован'}), 401

    student = Student.get_by_id(student_id)
    if not student:
        return jsonify({'error': 'Ученик не найден'}), 401

    refusal = _answer_refusal(student)
    if refusal:
        return jsonify({'error': refusal}), 403

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


# ==================== ИНТЕРФЕЙС УЧЕНИКА ====================

@bp.route('/test')
def student_login():
    """Страница входа ученика"""
    active_session = TestSession.get_active()
    if not active_session:
        return render_template('student/no_test.html')

    session_variants = _get_session_variant_options(active_session['id'])

    from flask import session
    student_id = session.get('student_id')
    if student_id:
        student = Student.get_by_id(student_id)
        if student and student['status'] == 'in_progress':
            test_session = TestSession.get_by_id(student['session_id'])
            if test_session and test_session['status'] == 'active':
                return redirect(url_for('student.student_test'))
            if test_session and test_session['status'] == 'closed':
                return redirect(url_for('student.student_result'))
        _clear_student_session()

    need_code = active_session['access_code'] is not None
    app_mode = request.args.get('app') == '1'
    autocomplete_section = secrets.token_hex(8)
    return render_template('student/login.html',
                           need_code=need_code,
                           active_session_id=active_session['id'],
                           app_mode=app_mode,
                           autocomplete_section=autocomplete_section,
                           multi_variant_mode=bool(session_variants),
                           session_variants=session_variants)


RELOGIN_BLOCK_SECONDS = 30  # открытое окно теста отмечается на сервере каждые 5 секунд


def _seen_recently(student):
    last_seen = _parse_dt(student.get('last_seen_at'))
    if not last_seen:
        return False
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last_seen).total_seconds() < RELOGIN_BLOCK_SECONDS


@bp.route('/test/start', methods=['POST'])
def student_start():
    """Начало теста учеником"""
    first_name = request.form.get('first_name', '').strip()
    last_name = request.form.get('last_name', '').strip()
    code = request.form.get('code', '').strip().upper()
    app_mode = request.form.get('app_mode') == '1'

    if not first_name or not last_name:
        flash('Введите имя и фамилию', 'error')
        return redirect(url_for('student.student_login'))

    active_session = TestSession.get_active()
    if not active_session:
        flash('Нет активного тестирования', 'error')
        return redirect(url_for('student.student_login'))

    # Проверка кода
    if active_session['access_code'] and active_session['access_code'] != code:
        log_event(f'{last_name} {first_name} ввёл неверный код доступа ({{pc}}).', 'warn', request.remote_addr)
        flash('Неверный код доступа', 'error')
        return redirect(url_for('student.student_login'))

    # Проверка на повторный вход
    existing = Student.get_by_session_and_name(active_session['id'], first_name, last_name)
    if existing:
        if existing['status'] == 'in_progress':
            from flask import session
            if session.get('student_id') != existing['id'] and _seen_recently(existing):
                log_event(f'{last_name} {first_name} пытался войти, но уже решает тест на другом компьютере ({{pc}}).', 'warn', request.remote_addr)
                flash(f'{last_name} {first_name} уже решает тест на другом компьютере. '
                      f'Если это вы и вы сменили компьютер, закройте старое окно и подождите '
                      f'{RELOGIN_BLOCK_SECONDS} секунд или обратитесь к учителю.', 'error')
                return redirect(url_for('student.student_login'))
            session['student_id'] = existing['id']
            session['start_time'] = (existing.get('started_at') or datetime.now().isoformat())
            session['time_limit'] = active_session['time_limit']
            session['app_mode'] = app_mode
            Student.touch(existing['id'])
            log_event(f'{last_name} {first_name} снова вошёл в тест — {{pc}}. Время продолжает идти.', 'info', request.remote_addr)
            return redirect(url_for('student.student_test'))
        flash('Вы уже завершили этот тест', 'error')
        return redirect(url_for('student.student_login'))

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
        variant_label = None
    else:
        session_variants = _get_session_variant_options(active_session['id'])
        if session_variants:
            chosen_position = request.form.get('variant_choice', type=int)
            if not chosen_position:
                flash('Выберите номер варианта', 'error')
                return redirect(url_for('student.student_login'))
            chosen_variant = TestSession.get_variant_by_position(active_session['id'], chosen_position)
            if not chosen_variant:
                flash('Выбран недоступный вариант', 'error')
                return redirect(url_for('student.student_login'))
            variant_id = chosen_variant['variant_id']
            variant_label = str(chosen_variant['position'])
        else:
            variant_id = active_session['variant_id']
            variant_label = None

    # Создаём запись ученика
    student_id = Student.create(active_session['id'], first_name, last_name, variant_id, variant_label=variant_label)
    Student.touch(student_id)
    log_event(f'{last_name} {first_name} вошёл в тест — {{pc}}. Время пошло.', 'ok', request.remote_addr)

    # Сохраняем в сессию
    from flask import session
    session['student_id'] = student_id
    session['start_time'] = datetime.now().isoformat()
    session['time_limit'] = active_session['time_limit']
    session['app_mode'] = app_mode

    return redirect(url_for('student.student_test'))


@bp.route('/test/exam')
def student_test():
    """Страница прохождения теста"""
    from flask import session

    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('student.student_login'))

    student = Student.get_by_id(student_id)
    if not student:
        _clear_student_session()
        return redirect(url_for('student.student_login'))
    if student['status'] == 'finished':
        return redirect(url_for('student.student_result'))  # учитель завершил тест этому ученику

    test_session = TestSession.get_by_id(student['session_id'])
    if test_session and test_session['status'] == 'closed':
        Student.finish(student_id)
        return redirect(url_for('student.student_result'))

    if student.get('submitted_at'):
        # Работа сдана: только заставка, без заданий и ответов. Оценка — после завершения тестирования.
        return render_template('student/submitted.html', student=student)

    # Получаем задачи варианта (при «вразброс» — в порядке этого ученика)
    tasks = _tasks_for_student(student, test_session)

    # Получаем уже сохранённые ответы
    answers = {}
    for task in tasks:
        ans = Answer.get_for_student_task(student_id, task['id'])
        if ans:
            answers[task['id']] = ans

    # Вычисляем оставшееся время по данным БД (устойчиво к перезапуску браузера)
    remaining = _calc_remaining_seconds(student, test_session)

    current_task = request.args.get('task', 0, type=int)

    # файлы заданий для редактора Python: программа ученика скачивает их в папку, где запускается код
    python_files = {str(task['id']): {'url': url_for('system.serve_attachment', filename=task['attachment_path']),
                                      'name': task.get('attachment_name') or task['attachment_path']}
                    for task in tasks if task.get('attachment_path')}

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
                          python_enabled=bool(test_session and test_session.get('python_enabled')),
                          student_app=_from_student_app(),  # в программе ученика Python — под заданием, на его компьютере
                          python_files=python_files,
                          mask_answers=bool(test_session and test_session.get('mask_answers')),
                          variant_label=student.get('variant_label'))


@bp.route('/test/save', methods=['POST'])
def student_save_answer():
    """Сохранение ответа ученика"""
    from flask import session

    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'error': 'Не авторизован'}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'error': 'Неверный запрос'}), 400
    task_id = data.get('task_id')

    student = Student.get_by_id(student_id)
    if not student:
        return jsonify({'error': 'Не авторизован'}), 401
    refusal = _answer_refusal(student)
    if refusal:
        return jsonify({'error': refusal, 'locked': True}), 403
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Неверное задание'}), 400
    if task_id not in {t['id'] for t in Variant.get_tasks(student['variant_id'])}:
        return jsonify({'error': 'Задание не из вашего варианта'}), 403
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


ANSWER_GRACE_SECONDS = 10  # ответы, отправленные в самый момент окончания времени, ещё принимаются


def _answer_refusal(student):
    """Почему ответ сейчас нельзя принять (или None). Проверяется на сервере, а не только в браузере."""
    if student['status'] != 'in_progress':
        return 'Тест уже завершён'
    if student.get('submitted_at'):
        return 'Работа уже сдана'
    test_session = TestSession.get_by_id(student['session_id'])
    if not test_session or test_session['status'] != 'active':
        return 'Тестирование завершено'
    if test_session.get('paused'):
        return 'Тестирование на паузе'
    if _calc_remaining_seconds(_shifted(student, ANSWER_GRACE_SECONDS), test_session) <= 0:
        return 'Время вышло'
    return None


def _shifted(student, seconds):
    """Копия ученика, начавшего на seconds секунд позже (для запаса на последние ответы)."""
    started = _parse_dt(student.get('started_at'))
    if not started:
        return student
    from datetime import timedelta
    copy = dict(student)
    copy['started_at'] = (started + timedelta(seconds=seconds)).isoformat(sep=' ')
    return copy


@bp.route('/test/finish', methods=['POST'])
def student_finish():
    """Завершение теста"""
    from flask import session

    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('student.student_login'))

    student = Student.get_by_id(student_id)
    if not student:
        return redirect(url_for('student.student_login'))

    test_session = TestSession.get_by_id(student['session_id'])
    if test_session and test_session.get('teacher_finish_only') and test_session['status'] != 'closed':
        return redirect(url_for('student.student_test'))

    Student.finish(student_id)

    return redirect(url_for('student.student_result'))


@bp.route('/test/ping', methods=['POST'])
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

    Student.touch(student_id, in_app=_from_student_app())
    remaining = _calc_remaining_seconds(student, test_session)

    if test_session['status'] == 'closed' or student['status'] == 'finished':
        # тестирование завершено для всех — или учитель завершил его только этому ученику
        if student['status'] != 'finished':
            Student.finish(student_id)
        return jsonify({'active': False, 'finished': True, 'redirect': url_for('student.student_result')})

    return jsonify({'active': True, 'paused': bool(test_session.get('paused')), 'remaining_seconds': remaining,
                    'submitted': bool(student.get('submitted_at'))})


STUDENT_APP_MARK = 'TestingStudentApp/'  # программа ученика (student_app) дописывает это к User-Agent


def _from_student_app():
    return STUDENT_APP_MARK in (request.headers.get('User-Agent') or '')


TASK_TIME_PORTION_MAX = 60  # страница шлёт время раз в 10 секунд; больше минуты за раз — сбой или подделка


def _parse_task_times(raw, allowed_ids):
    """'{"12": 7, "15": 3}' -> {12: 7, 15: 3}: только задания своего варианта, всего не больше минуты."""
    try:
        data = json.loads(raw or '{}')
    except ValueError:
        return {}
    times, total = {}, 0
    if not isinstance(data, dict):
        return {}
    for key, value in data.items():
        try:
            task_id, seconds = int(key), int(value)
        except (TypeError, ValueError):
            continue
        seconds = min(seconds, TASK_TIME_PORTION_MAX - total)
        if task_id in allowed_ids and seconds > 0:
            times[task_id] = seconds
            total += seconds
    return times


@bp.route('/test/away', methods=['POST'])
def student_away():
    """Страница теста: ушёл (away=1) / вернулся (away=0) и порция времени по заданиям (times).
    Учитывается только в ходе работы."""
    from flask import session
    student = Student.get_by_id(session.get('student_id') or 0)
    if not student:
        return ('', 204)
    test_session = TestSession.get_by_id(student['session_id'])
    working = (student['status'] == 'in_progress' and test_session and test_session['status'] == 'active'
               and not test_session.get('paused') and not student.get('submitted_at'))
    away = request.values.get('away')
    if away == '0' or (away == '1' and working):  # возврат учитываем всегда, чтобы не потерять время отсутствия
        Student.set_away(student['id'], away == '1')
    # программа ученика сообщает, какую программу он пытался открыть (окно теста она уже вернула)
    app_name = (request.values.get('app') or '').strip()[:60]
    if working and away == '1' and app_name:
        Student.set_foreign_app(student['id'], app_name)
        log_event(f"{student['last_name']} {student['first_name']} пытался открыть «{app_name}» — {{pc}}.", 'warn',
                  request.remote_addr)
    if working and request.values.get('times'):
        allowed = {task['id'] for task in Variant.get_tasks(student['variant_id'])}
        Student.add_task_times(student['id'], _parse_task_times(request.values.get('times'), allowed))
    return ('', 204)


@bp.route('/test/submit', methods=['POST'])
def student_submit():
    """Ученик сдаёт работу: ответы больше не меняются, на экране заставка до завершения тестирования."""
    from flask import session
    student = Student.get_by_id(session.get('student_id') or 0)
    if not student:
        return redirect(url_for('student.student_login'))
    test_session = TestSession.get_by_id(student['session_id'])
    if student['status'] == 'in_progress' and test_session and test_session['status'] == 'active' and not student.get('submitted_at'):
        Student.set_submitted(student['id'], True)
        log_event(f"{student['last_name']} {student['first_name']} сдал работу — {{pc}}.", 'ok', request.remote_addr)
    return redirect(url_for('student.student_test'))


@bp.route('/test/run-python', methods=['POST'])
def student_run_python():
    """Запуск Python-кода на компьютере учителя.

    Код выполняется реальным Python с доступом к файлу задачи.
    Ошибки возвращаются в точном формате IDLE (traceback as-is).
    """
    import ast
    import json
    import shutil
    import subprocess
    import sys
    import tempfile
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

    # Разрешаем обычные модули стандартной библиотеки, но блокируем опасные корни.
    blocked_import_roots = {
        'asyncio', 'builtins', 'code', 'codecs', 'codeop', 'compileall',
        'concurrent', 'ctypes', 'dbm', 'filecmp', 'fileinput', 'ftplib',
        'glob', 'http', 'imaplib', 'importlib', 'inspect', 'io', 'mailbox',
        'marshal', 'mmap', 'multiprocessing', 'os', 'pathlib', 'pickle',
        'poplib', 'posix', 'pty', 'resource', 'runpy', 'selectors', 'shlex',
        'shutil', 'signal', 'site', 'smtplib', 'socket', 'socketserver',
        'ssl', 'subprocess', 'sys', 'tempfile', 'telnetlib', 'threading',
        'urllib', 'webbrowser', 'xmlrpc', 'zipfile', 'zipimport'
    }
    stdlib_roots = set(getattr(sys, 'stdlib_module_names', set()))
    allowed_import_roots = {
        root for root in stdlib_roots
        if root and not root.startswith('_') and root not in blocked_import_roots
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


@bp.route('/test/result')
def student_result():
    """Результат ученика"""
    from flask import session as flask_session

    student_id = flask_session.get('student_id')
    if not student_id:
        return redirect(url_for('student.student_login'))

    student = Student.get_by_id(student_id)
    if not student:
        _clear_student_session()
        return redirect(url_for('student.student_login'))

    test_session = TestSession.get_by_id(student['session_id'])

    session_open = bool(test_session) and test_session['status'] != 'closed'
    if session_open and test_session.get('teacher_finish_only') and student['status'] != 'finished':
        return redirect(url_for('student.student_test'))

    if student['status'] != 'finished':
        Student.finish(student_id)

    # Получаем все ответы
    answers = Student.get_answers(student_id)
    tasks = _tasks_for_student(student, test_session)

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
    _clear_student_session()

    return render_template('student/result.html',
                          student=student,
                          answers=answers,
                          answers_dict=answers_dict,
                          tasks=tasks,
                          correct_count=correct_count,
                          total=total,
                          grade=grade,
                          # правильные ответы — только когда учитель завершил тестирование для всех:
                          # пока кто-то решает, ученик перескажет их соседям
                          show_answers=bool(test_session) and not session_open,
                          variant_label=student.get('variant_label'))
