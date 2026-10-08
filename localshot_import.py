"""
Чтение файла варианта LocalShot (.variant).

Файл .variant — обычный ZIP-архив:
    manifest.json          {"version": 1, "name": "...", "tasks": [{"id", "mode", "answers", "attachment_name"?}]}
    images/<id>.png        скриншот задания
    files/<id>.bin         файл к заданию (если есть)

Модуль только читает и проверяет файл, ничего не сохраняя: если файл неправильный,
ошибка возникает до того, как в системе создано хоть что-то.
"""
import json
import re
import zipfile
import zlib
from dataclasses import dataclass, field

from config import (ALLOWED_ATTACHMENT_EXTENSIONS, MAX_ATTACHMENT_SIZE,
                    MAX_LOCALSHOT_FILE_SIZE, MAX_LOCALSHOT_IMAGE_SIZE, MAX_LOCALSHOT_TASKS)

SUPPORTED_FORMAT_VERSION = 1
MAX_ANSWERS = 20
MAX_NAME_LENGTH = 80
MODE_FIELDS = 'fields'        # каждый ответ в своём поле
MODE_MULTILINE = 'multiline'  # один ответ из нескольких строк (задания 25, 27)

_TASK_ID = re.compile(r'[0-9a-f]{6,32}')
_BAD_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_ZIP_LIKE = {'xlsx', 'ods', 'odt', 'docx', 'zip'}
_TEXT_LIKE = {'txt', 'csv', 'py'}


class VariantFileError(Exception):
    """Понятная учителю причина, почему файл нельзя загрузить."""


@dataclass
class ImportedTask:
    number: int
    mode: str
    answers: list
    image: bytes
    attachment_name: str = None
    attachment: bytes = None


@dataclass
class ImportedVariant:
    name: str
    tasks: list = field(default_factory=list)
    catalog: dict = None  # «паспорт»: класс, тема, комплект, время (если файл его содержит)


def clean_name(name, default):
    name = _BAD_NAME_CHARS.sub(' ', str(name or ''))
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return name[:MAX_NAME_LENGTH] or default


def _extension(filename):
    return filename.rsplit('.', 1)[1].lower() if '.' in filename else ''


def check_attachment(number, name, data):
    ext = _extension(name)
    if ext not in ALLOWED_ATTACHMENT_EXTENSIONS:
        allowed = ', '.join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS))
        raise VariantFileError(f'Задание {number}: файл «{name}» такого типа не принимается. '
                               f'Допустимые типы: {allowed}. Замените файл в LocalShot.')
    broken = False
    if ext in _ZIP_LIKE:
        broken = data[:4] not in (b'PK\x03\x04', b'PK\x05\x06')
    elif ext == 'pdf':
        broken = not data.startswith(b'%PDF')
    elif ext in _TEXT_LIKE:
        broken = b'\x00' in data[:1024]
    if broken:
        raise VariantFileError(f'Задание {number}: файл «{name}» повреждён или не соответствует своему типу.')


def _read_limited(archive, name, limit, what):
    """Читает часть архива, не доверяя размеру, который заявлен в самом архиве."""
    try:
        with archive.open(name) as stream:
            data = stream.read(limit + 1)
    except KeyError:
        raise VariantFileError(f'Файл варианта повреждён: нет части «{what}».') from None
    except (zipfile.BadZipFile, RuntimeError, OSError, NotImplementedError, ValueError, EOFError, zlib.error) as error:
        raise VariantFileError(f'Файл варианта повреждён: не читается «{what}».') from error
    if len(data) > limit:
        raise VariantFileError(f'Файл варианта не подходит: «{what}» слишком большой.')
    return data


def parse_variant_file(stream, fallback_name='Вариант'):
    """Проверяет файл .variant и возвращает ImportedVariant. Ничего не сохраняет."""
    try:
        archive = zipfile.ZipFile(stream)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise VariantFileError('Это не файл варианта LocalShot или он повреждён.') from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_LOCALSHOT_TASKS * 2 + 10:
            raise VariantFileError('В файле слишком много частей: это не вариант LocalShot.')
        if sum(info.file_size for info in infos) > MAX_LOCALSHOT_FILE_SIZE:
            raise VariantFileError('Вариант слишком большой (больше 200 МБ после распаковки).')

        raw = _read_limited(archive, 'manifest.json', 5 * 1024 * 1024, 'manifest.json')
        try:
            manifest = json.loads(raw.decode('utf-8-sig'))
        except (ValueError, UnicodeDecodeError):
            raise VariantFileError('Это не файл варианта LocalShot: не читается описание заданий.') from None
        if not isinstance(manifest, dict) or not isinstance(manifest.get('tasks'), list):
            raise VariantFileError('Это не файл варианта LocalShot.')
        version = manifest.get('version', 0)
        if not isinstance(version, int) or version > SUPPORTED_FORMAT_VERSION:
            raise VariantFileError('Вариант создан более новой версией LocalShot. Обновите систему тестирования.')
        items = manifest['tasks']
        if not items:
            raise VariantFileError('В варианте нет ни одного задания.')
        if len(items) > MAX_LOCALSHOT_TASKS:
            raise VariantFileError(f'В варианте больше {MAX_LOCALSHOT_TASKS} заданий.')

        result = ImportedVariant(name=clean_name(manifest.get('name'), clean_name(fallback_name, 'Вариант')),
                                 catalog=manifest.get('catalog') if isinstance(manifest.get('catalog'), dict) else None)
        seen_ids = set()
        total_bytes = 0
        for number, item in enumerate(items, 1):
            if not isinstance(item, dict):
                raise VariantFileError(f'Задание {number}: неверное описание.')
            task_id = str(item.get('id', ''))
            if not _TASK_ID.fullmatch(task_id) or task_id in seen_ids:
                raise VariantFileError(f'Задание {number}: файл варианта повреждён (неверный номер задания).')
            seen_ids.add(task_id)

            mode = item.get('mode', MODE_FIELDS)
            if mode not in (MODE_FIELDS, MODE_MULTILINE):
                raise VariantFileError(f'Задание {number}: неизвестный тип ответа «{mode}».')
            answers = item.get('answers')
            if not isinstance(answers, list) or not answers or len(answers) > MAX_ANSWERS:
                raise VariantFileError(f'Задание {number}: неверное число ответов.')
            answers = [str(answer).strip() for answer in answers]
            if any(not answer for answer in answers):
                raise VariantFileError(f'Задание {number}: есть пустой ответ.')
            if mode == MODE_MULTILINE and len(answers) != 1:
                raise VariantFileError(f'Задание {number}: ответ из нескольких строк должен быть одним текстом.')

            image = _read_limited(archive, f'images/{task_id}.png', MAX_LOCALSHOT_IMAGE_SIZE,
                                  f'картинка задания {number}')
            if not image.startswith(b'\x89PNG\r\n\x1a\n'):
                raise VariantFileError(f'Задание {number}: картинка не является PNG.')

            attachment = attachment_name = None
            if item.get('attachment_name'):
                attachment_name = clean_name(str(item['attachment_name']).replace('\\', '/').rsplit('/', 1)[-1], 'файл')
                attachment = _read_limited(archive, f'files/{task_id}.bin', MAX_ATTACHMENT_SIZE,
                                           f'файл к заданию {number}')
                check_attachment(number, attachment_name, attachment)

            total_bytes += len(image) + len(attachment or b'')
            if total_bytes > MAX_LOCALSHOT_FILE_SIZE:
                raise VariantFileError('Вариант слишком большой (больше 200 МБ).')
            result.tasks.append(ImportedTask(number, mode, answers, image, attachment_name, attachment))
    return result
