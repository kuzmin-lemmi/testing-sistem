"""Система: главная страница, состояние и остановка, файлы заданий, ярлык ученика, настройки."""
import os

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)

import activity
import variant_folder
from activity import log_event
from blueprints.common import PYODIDE_DIR, _get_session_display_name, get_local_ip, grade_percents
from config import ATTACHMENTS_DIR, BACKUPS_DIR, IMAGES_DIR, PORT, SHOW_ACCESS_CODE_OPTION, SHOW_EGE_BANKS
from models import (
    GradeCriteria,
    Task,
    TestSession,
    Variant,
    backup_database,
    get_setting,
    known_variant_files,
    list_backups,
    set_setting,
)
from student_shortcut import student_urls

bp = Blueprint('system', __name__)


# ==================== ГЛАВНАЯ СТРАНИЦА ====================

@bp.route('/')
def index():
    """Главная страница учителя"""
    active_session = TestSession.get_active()
    return render_template('teacher/index.html',
                         local_ip=get_local_ip(),
                         port=PORT,
                         student_urls=student_urls(PORT),
                         status=_system_status(),
                         active_session=active_session,
                         active_session_name=_get_session_display_name(active_session) if active_session else None,
                         sessions_count=len(TestSession.get_all()),
                         variants_count=len(Variant.get_all()))


def primary_student_url():
    """Один адрес, который ученики вводят в браузере (остальные — в подробностях)."""
    return f'http://{get_local_ip()}:{PORT}/test'


def _system_status():
    active = TestSession.get_active()
    return {
        'address': primary_student_url(),
        'other_addresses': [u for u in student_urls(PORT) if u != primary_student_url().rsplit('/test', 1)[0]],
        'firewall': activity.firewall_status(),
        'computers': activity.students_seen_count(),
        'uptime': activity.uptime_text(),
        'active': bool(active),
        'events': activity.recent_events(40),
    }


@bp.route('/system/status')
def system_status():
    """Данные для блока «Состояние системы» на главной странице (страница обновляет их сама)."""
    return jsonify(_system_status())


@bp.route('/system/firewall', methods=['POST'])
def system_firewall():
    """Открыть доступ ученикам в брандмауэре Windows. Windows покажет учителю окно «Разрешить?»."""
    if activity.open_firewall():
        log_event('Доступ для учеников в брандмауэре Windows открыт.', 'ok')
        return jsonify({'ok': True})
    log_event('Не удалось открыть доступ в брандмауэре Windows (возможно, вы нажали «Нет»).', 'warn')
    return jsonify({'ok': False}), 200


@bp.route('/system/log')
def system_log():
    """Файл технического журнала: его можно отправить разработчику, если что-то не работает."""
    if not os.path.exists(activity.LOG_FILE):
        flash('Журнал пока пуст', 'info')
        return redirect(url_for('system.index'))
    return send_file(activity.LOG_FILE, mimetype='text/plain', as_attachment=True, download_name='журнал-системы.txt')


@bp.route('/system/stop', methods=['POST'])
def system_stop():
    """Остановить систему. Без неё тест у учеников прервётся, поэтому страница спрашивает подтверждение."""
    log_event('Система остановлена учителем.', 'warn')
    import threading
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return render_template('teacher/stopped.html')


@bp.route('/static/pyodide/<path:filename>')
def pyodide_file(filename):
    """Python-редактор для учеников. Лежит отдельной папкой (она большая), в программу не вшивается."""
    return send_from_directory(PYODIDE_DIR, filename)


@bp.route('/shortcut/download')
def student_shortcut_download():
    """Файл «Тестирование.html» для рабочих столов учеников: ждёт запуска системы и сам открывает вход."""
    import io
    html = render_template('shortcut/wait.html', addresses=student_urls(PORT))
    return send_file(io.BytesIO(html.encode('utf-8')), mimetype='text/html', as_attachment=True,
                     download_name='Тестирование.html')


# ==================== ФАЙЛЫ ====================

@bp.route('/images/<filename>')
def serve_image(filename):
    """Отдача изображений"""
    return send_from_directory(IMAGES_DIR, filename)


@bp.route('/attachments/<filename>')
def serve_attachment(filename):
    """Отдача прикреплённых файлов"""
    return send_from_directory(ATTACHMENTS_DIR, filename, as_attachment=True,
                               download_name=Task.get_attachment_name(filename) or filename)


# ==================== НАСТРОЙКИ ====================

@bp.route('/settings', methods=['GET', 'POST'])
def settings():
    """Настройки: оценки по умолчанию, ярлык для учеников, копии базы, скрытые разделы."""
    if request.method == 'POST':
        try:
            values = [int(request.form.get(f'grade_{g}_percent', '')) for g in (5, 4, 3)]
        except ValueError:
            values = None
        if not values or not all(1 <= v <= 100 for v in values) or not values[0] >= values[1] >= values[2]:
            flash('Проценты должны быть от 1 до 100 и не возрастать: «5» ≥ «4» ≥ «3»', 'error')
        else:
            for grade, value in zip((5, 4, 3), values):
                set_setting(f'grade_{grade}_percent', value)
            set_setting('show_ege_banks', '1' if request.form.get('show_ege_banks') == '1' else '0')
            set_setting('show_access_code', '1' if request.form.get('show_access_code') == '1' else '0')
            flash('Настройки сохранены', 'success')
        return redirect(url_for('system.settings'))
    return render_template('teacher/settings.html', percents=grade_percents(),
                           criteria=GradeCriteria.get_all(), backups=list_backups(BACKUPS_DIR)[:10],
                           backups_dir=BACKUPS_DIR, student_urls=student_urls(PORT), port=PORT,
                           ege_banks_forced=bool(SHOW_EGE_BANKS), access_code_forced=bool(SHOW_ACCESS_CODE_OPTION),
                           ege_banks_on=get_setting('show_ege_banks') == '1',
                           access_code_on=get_setting('show_access_code') == '1',
                           variants_dir=variant_folder.folder(get_setting),
                           variants_dir_info=_variants_dir_info())


def _variants_dir_info():
    root = variant_folder.folder(get_setting)
    if not root or not os.path.isdir(root):
        return {'exists': False}
    names = {v['name'] for v in Variant.get_all()}
    return {'exists': True, 'files': len(variant_folder.variant_files(root)),
            'new': len(variant_folder.new_files(root, known_variant_files(), names))}


@bp.route('/settings/variants-dir', methods=['POST'])
def settings_variants_dir():
    path = (request.form.get('variants_dir') or '').strip().strip('"')
    if path and not os.path.isdir(path):
        flash(f'Папка не найдена: {path}', 'error')
        return redirect(url_for('system.settings') + '#variants-dir')
    set_setting(variant_folder.SETTING, path)
    flash('Папка с вариантами сохранена' if path else 'Папка с вариантами сброшена — будет использоваться D:\\Варианты', 'success')
    return redirect(url_for('system.settings') + '#variants-dir')


@bp.route('/settings/pick-folder', methods=['POST'])
def settings_pick_folder():
    """Открывает на компьютере учителя окно Windows «Выбор папки» и возвращает выбранный путь."""
    start = ((request.get_json(silent=True) or {}).get('start') or '').strip()
    path = variant_folder.pick_folder(start if os.path.isdir(start) else '')
    return jsonify({'path': path} if path else {'path': '', 'error': ''})


@bp.route('/settings/backup', methods=['POST'])
def settings_backup():
    try:
        path = backup_database(BACKUPS_DIR)
    except Exception as error:
        current_app.logger.exception('Не удалось сделать копию базы')
        flash(f'Не удалось сделать копию: {error}', 'error')
    else:
        flash(f'Копия сохранена: {os.path.basename(path)}', 'success')
    return redirect(url_for('system.settings'))


@bp.route('/settings/criteria', methods=['POST'])
def update_criteria():
    """Обновление критериев оценки"""
    name = request.form.get('name')
    total_tasks = int(request.form.get('total_tasks'))
    grade_5_min = int(request.form.get('grade_5_min'))
    grade_4_min = int(request.form.get('grade_4_min'))
    grade_3_min = int(request.form.get('grade_3_min'))

    GradeCriteria.create_or_update(name, total_tasks, grade_5_min, grade_4_min, grade_3_min)
    flash('Критерии оценки сохранены', 'success')
    return redirect(url_for('system.settings'))
