"""
Система тестирования ЕГЭ по информатике
Главный файл сервера
"""
from flask import Flask, request

import activity
from activity import log_event
from blueprints import student, system, teacher, variants
from blueprints.common import (
    _check_csrf,
    _error_response,
    _get_csrf_token,
    _is_teacher_request_path,
    _normalize_ip,
    _request_from_teacher_machine,
    show_ege_banks,
    to_local_dt,
)
from blueprints.system import primary_student_url
from config import BACKUPS_DIR, HOST, PORT, SECRET_KEY
from models import backup_database, init_db, migrate_db
from results_logic import expected_answers, format_away, format_expected, format_given, plural, tasks_word

app = Flask(__name__)
app.secret_key = SECRET_KEY
for _module in (system, teacher, variants, student):
    app.register_blueprint(_module.bp)


@app.after_request
def add_no_store_headers(response):
    if request.path.startswith('/test'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.errorhandler(Exception)
def handle_exception(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error
    app.logger.exception('Unhandled exception')
    log_event('В системе произошла ошибка. Подробности записаны в файл журнала (Настройки → Журнал).', 'error', request.remote_addr)
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
try:
    from models import recheck_wrong_answers_once
    _fixed = recheck_wrong_answers_once()  # «3В8» с русской «В» теперь засчитывается — исправляем старые результаты
    if _fixed:
        app.logger.info('Перепроверка ответов: исправлено %s', _fixed)
except Exception:
    app.logger.exception('Не удалось перепроверить старые ответы')
try:
    from models import autofill_catalog_once
    autofill_catalog_once()  # старые варианты раскладываются по каталогу по названиям (один раз)
except Exception:
    app.logger.exception('Не удалось разложить варианты по каталогу')


@app.template_filter('local_dt')
def local_dt_filter(value):
    return to_local_dt(value, with_seconds=False)


@app.template_filter('local_date')
def local_date_filter(value):
    local = to_local_dt(value, with_seconds=False)
    return local[:10] if local else ''


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': _get_csrf_token, 'show_ege_banks': show_ege_banks(),
            'format_expected': format_expected, 'format_given': format_given, 'expected_answers': expected_answers,
            'plural': plural, 'tasks_word': tasks_word, 'format_away': format_away}


@app.before_request
def track_student_computers():
    if request.path.startswith('/test') and not _request_from_teacher_machine():
        activity.note_student_contact(_normalize_ip(request.remote_addr))
    return None


@app.before_request
def protect_teacher_routes():
    if not _is_teacher_request_path(request.path):
        return None

    if not _request_from_teacher_machine():
        return _error_response('Доступ к панели учителя разрешён только с компьютера учителя', 403)

    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and not _check_csrf():
        return _error_response('Недействительный CSRF-токен', 400)

    return None


# ==================== ЗАПУСК ====================

def prepare_startup():
    """Копия базы и первая запись в журнал. Вызывается и из launcher.py, и при запуске `python server.py`."""
    try:
        backup_database(BACKUPS_DIR)  # копия базы при каждом запуске, хранятся последние 20
    except Exception:
        app.logger.exception('Не удалось сделать копию базы')
        log_event('Не удалось сделать копию базы данных. Подробности — в журнале.', 'warn')
    log_event(f'Система запущена. Ученики заходят по адресу {primary_student_url()}', 'ok')


if __name__ == '__main__':
    activity.setup_logging()
    prepare_startup()
    print(f'Система запущена: http://127.0.0.1:{PORT}  (ученикам: {primary_student_url()})')
    print('Для остановки нажмите Ctrl+C. Подробности работы — в файле', activity.LOG_FILE)
    app.run(host=HOST, port=PORT, debug=False, threaded=True)
