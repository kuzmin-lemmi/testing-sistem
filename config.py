"""
Конфигурация системы тестирования ЕГЭ
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Пути к данным
DATA_DIR = os.path.join(BASE_DIR, 'data')
DATABASE_PATH = os.path.join(DATA_DIR, 'database.db')
IMAGES_DIR = os.path.join(DATA_DIR, 'images')
ATTACHMENTS_DIR = os.path.join(DATA_DIR, 'attachments')
EXPORTS_DIR = os.path.join(BASE_DIR, 'exports')

# Настройки сервера
HOST = '0.0.0.0'  # Доступ из локальной сети
PORT = 8080

# Ограничения файлов
MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5 МБ
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 МБ
MAX_IMPORT_ZIP_SIZE = 50 * 1024 * 1024  # 50 МБ
ALLOWED_IMAGE_EXTENSIONS = {'png'}
ALLOWED_ATTACHMENT_EXTENSIONS = {'txt', 'xlsx', 'csv'}

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
for directory in [DATA_DIR, IMAGES_DIR, ATTACHMENTS_DIR, EXPORTS_DIR]:
    os.makedirs(directory, exist_ok=True)
