"""
Конфигурация системы тестирования ЕГЭ
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Пути к данным
DATA_DIR = os.environ.get('EGE_DATA_DIR') or os.path.join(BASE_DIR, 'data')  # EGE_DATA_DIR — для автоматических тестов
DATABASE_PATH = os.path.join(DATA_DIR, 'database.db')
IMAGES_DIR = os.path.join(DATA_DIR, 'images')
ATTACHMENTS_DIR = os.path.join(DATA_DIR, 'attachments')
STUDENT_UPLOADS_DIR = os.path.join(DATA_DIR, 'student_uploads')
EXPORTS_DIR = os.path.join(BASE_DIR, 'exports')

# Настройки сервера
HOST = '0.0.0.0'  # Доступ из локальной сети
PORT = 8080

# Вход учеников по коду доступа: в интерфейсе скрыт, сама возможность осталась.
# Чтобы снова показать галочку «Требовать код для входа» при запуске тестирования, задайте EGE_ACCESS_CODE=1.
SHOW_ACCESS_CODE_OPTION = os.environ.get('EGE_ACCESS_CODE', '') == '1'

# Банки заданий по номерам ЕГЭ (банк задач, тематические и индивидуальные варианты): скрыты из интерфейса,
# данные и страницы остались. Вернуть: переменная окружения EGE_BANKS=1 (или True здесь).
SHOW_EGE_BANKS = os.environ.get('EGE_BANKS', '') == '1'

# IP-адреса, которым разрешён доступ к панели учителя
# По умолчанию: только текущий компьютер (localhost)
_teacher_ips_raw = os.environ.get('TEACHER_ALLOWED_IPS', '127.0.0.1,::1')
TEACHER_ALLOWED_IPS = {
    ip.strip() for ip in _teacher_ips_raw.split(',') if ip.strip()
}

# Ограничения файлов
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 МБ
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 МБ
MAX_IMPORT_ZIP_SIZE = 50 * 1024 * 1024  # 50 МБ
MAX_STUDENT_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 МБ
ALLOWED_STUDENT_UPLOAD_EXTENSIONS = {'ods', 'odt'}
ALLOWED_IMAGE_EXTENSIONS = {'png'}
ALLOWED_ATTACHMENT_EXTENSIONS = {'txt', 'xlsx', 'xls', 'ods', 'csv', 'odt', 'docx', 'pdf', 'py', 'zip'}

# Загрузка варианта из LocalShot (.variant): скриншот всего экрана бывает больше 5 МБ
MAX_LOCALSHOT_FILE_SIZE = 200 * 1024 * 1024
MAX_LOCALSHOT_IMAGE_SIZE = 30 * 1024 * 1024
MAX_LOCALSHOT_TASKS = 200

# Количество ответов по умолчанию для номеров ЕГЭ
DEFAULT_ANSWER_COUNT = {
    **{i: 1 for i in range(1, 17)},   # 1-16: один ответ
    17: 2,  # 17: два ответа
    18: 2,  # 18: два ответа
    19: 1,  # 19: один ответ
    20: 2,  # 20: два ответа (бывает 1)
    21: 1,  # 21: один ответ
    22: 1,  # 22: один ответ
    23: 1,  # 23: один ответ
    24: 1,  # 24: один ответ
    25: 0,  # 25: особый формат (n строк по 2 числа) - обрабатывается отдельно
    26: 2,  # 26: два ответа
    27: 0,  # 27: особый формат (2 строки по 2 числа) - обрабатывается отдельно
}

# Задания с особым форматом ответа
SPECIAL_ANSWER_FORMAT = {
    25: {'type': 'multiline', 'description': 'N строк, в каждой по 2 числа через пробел'},
    27: {'type': 'multiline_fixed', 'lines': 2, 'description': '2 строки, в каждой по 2 числа через пробел'},
}

# Создаём директории если не существуют
for directory in [DATA_DIR, IMAGES_DIR, ATTACHMENTS_DIR, STUDENT_UPLOADS_DIR, EXPORTS_DIR]:
    os.makedirs(directory, exist_ok=True)


def _load_secret_key():
    """Свой случайный ключ для каждой установки (им подписывается «пропуск» ученика в браузере).
    Создаётся при первом запуске и хранится в data/secret_key: стандартный ключ позволял подделать пропуск."""
    if os.environ.get('EGE_SECRET_KEY'):
        return os.environ['EGE_SECRET_KEY']
    import secrets
    path = os.path.join(DATA_DIR, 'secret_key')
    try:
        with open(path, encoding='utf-8') as stream:
            key = stream.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(48)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(key)
    return key


SECRET_KEY = _load_secret_key()
BACKUPS_DIR = os.path.join(DATA_DIR, 'backups')
