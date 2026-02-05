"""
Система тестирования ЕГЭ по информатике
Главный файл сервера
"""
from flask import Flask, render_template, request, redirect, url_for, jsonify, send_from_directory, send_file, flash
import os
import uuid
import socket
from werkzeug.utils import secure_filename
from datetime import datetime

from config import (HOST, PORT, DATA_DIR, IMAGES_DIR, ATTACHMENTS_DIR, EXPORTS_DIR,
                   MAX_IMAGE_SIZE, MAX_ATTACHMENT_SIZE, MAX_IMPORT_ZIP_SIZE,
                   ALLOWED_IMAGE_EXTENSIONS, ALLOWED_ATTACHMENT_EXTENSIONS,
                   DEFAULT_ANSWER_COUNT, SPECIAL_ANSWER_FORMAT)
from models import init_db, migrate_db, Task, Variant, GradeCriteria, TestSession, Student, Answer

app = Flask(__name__)
app.secret_key = 'ege-testing-secret-key-2026'

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
    task_counts = Task.count_by_ege_number()
    selected_ege = request.args.get('ege', type=int)
    
    tasks = []
    if selected_ege:
        tasks = Task.get_by_ege_number(selected_ege)
    
    return render_template('teacher/tasks.html',
                         task_counts=task_counts,
                         selected_ege=selected_ege,
                         tasks=tasks,
                         default_answer_count=DEFAULT_ANSWER_COUNT)

@app.route('/tasks/add', methods=['GET', 'POST'])
def task_add():
    """Добавление новой задачи"""
    if request.method == 'POST':
        ege_number = int(request.form.get('ege_number'))
        
        # Обработка изображения
        image = request.files.get('image')
        if not image or not allowed_file(image.filename, ALLOWED_IMAGE_EXTENSIONS):
            flash('Необходимо загрузить PNG-изображение', 'error')
            return redirect(url_for('task_add'))
        
        image_filename = generate_unique_filename(image.filename)
        image_path = os.path.join(IMAGES_DIR, image_filename)
        image.save(image_path)
        
        # Обработка прикреплённого файла (опционально)
        attachment_path = None
        attachment_name = None
        attachment = request.files.get('attachment')
        if attachment and attachment.filename:
            if allowed_file(attachment.filename, ALLOWED_ATTACHMENT_EXTENSIONS):
                attachment_name = secure_filename(attachment.filename)
                attachment_filename = generate_unique_filename(attachment.filename)
                attachment_path = os.path.join(ATTACHMENTS_DIR, attachment_filename)
                attachment.save(attachment_path)
        
        # Обработка ответов в зависимости от номера ЕГЭ
        answer_1 = None
        answer_2 = None
        answer_text = None
        answer_count = 1
        
        if ege_number in SPECIAL_ANSWER_FORMAT:
            # Особый формат (№25, №27)
            answer_text = request.form.get('answer_text', '').strip()
            answer_count = 0
        else:
            # Стандартный формат
            answer_count = int(request.form.get('answer_count', 1))
            # Разрешаем ответы не только числами: цифры/латиница/кириллица и т.п.
            # Храним как строку (SQLite типы динамические), а сравнение нормализуем в Answer.save().
            answer_1 = (request.form.get('answer_1') or '').strip() or None
            
            if answer_count == 2:
                answer_2 = (request.form.get('answer_2') or '').strip() or None
        
        Task.create(
            ege_number=ege_number,
            image_path=image_filename,
            answer_1=answer_1,
            answer_count=answer_count,
            answer_2=answer_2,
            attachment_path=attachment_filename if attachment_path else None,
            attachment_name=attachment_name,
            answer_text=answer_text
        )
        
        flash(f'Задача для номера {ege_number} успешно добавлена', 'success')
        return redirect(url_for('tasks_list', ege=ege_number))
    
    ege_number = request.args.get('ege', type=int, default=1)
    return render_template('teacher/task_add.html',
                         ege_number=ege_number,
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
        
        # Обновление номера ЕГЭ
        ege_number = request.form.get('ege_number')
        if ege_number:
            updates['ege_number'] = int(ege_number)
        
        # Обновление ответов
        answer_count = int(request.form.get('answer_count', 1))
        updates['answer_count'] = answer_count

        if answer_count == 0:
            # Текстовый/многострочный ответ (например, №25/27)
            updates['answer_text'] = (request.form.get('answer_text') or '').strip() or None
            updates['answer_1'] = None
            updates['answer_2'] = None
        else:
            # Разрешаем алфавитно-цифровые ответы
            updates['answer_1'] = (request.form.get('answer_1') or '').strip() or None
            updates['answer_2'] = (request.form.get('answer_2') or '').strip() or None if answer_count == 2 else None
            updates['answer_text'] = None
        
        # Обновление изображения (если загружено новое)
        image = request.files.get('image')
        if image and image.filename and allowed_file(image.filename, ALLOWED_IMAGE_EXTENSIONS):
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
                if allowed_file(attachment.filename, ALLOWED_ATTACHMENT_EXTENSIONS):
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
        return redirect(url_for('tasks_list', ege=updates.get('ege_number', task['ege_number'])))
    
    return render_template('teacher/task_edit.html', 
                         task=task,
                         default_answer_count=DEFAULT_ANSWER_COUNT)

@app.route('/tasks/<int:task_id>/delete', methods=['POST'])
def task_delete(task_id):
    """Удаление задачи"""
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
    
    return redirect(url_for('tasks_list'))


@app.route('/tasks/bulk', methods=['GET', 'POST'])
def tasks_bulk_upload():
    """Массовая загрузка задач"""
    if request.method == 'POST':
        ege_number = int(request.form.get('ege_number'))
        
        # Получаем данные о загруженных файлах
        task_data = []
        index = 0
        while True:
            image_path = request.form.get(f'image_path_{index}')
            if not image_path:
                break
            
            answer_1 = request.form.get(f'answer_1_{index}')
            answer_2 = request.form.get(f'answer_2_{index}')
            answer_text = request.form.get(f'answer_text_{index}')
            
            # Разрешаем ответы не только числами (кириллица/латиница/цифры)
            answer_1 = (answer_1 or '').strip() or None
            answer_2 = (answer_2 or '').strip() or None
            
            # Определяем количество ответов
            if ege_number in [25, 27]:
                answer_count = 0  # особый формат
            elif answer_2 is not None and str(answer_2).strip() != '':
                answer_count = 2
            else:
                answer_count = 1
            
            Task.create(
                ege_number=ege_number,
                image_path=image_path,
                answer_1=answer_1,
                answer_2=answer_2,
                answer_count=answer_count,
                answer_text=answer_text
            )
            index += 1
        
        flash(f'Добавлено {index} задач для номера {ege_number}', 'success')
        return redirect(url_for('tasks_list', ege=ege_number))
    
    # GET - показываем форму
    ege_number = request.args.get('ege', type=int, default=1)
    return render_template('teacher/tasks_bulk.html',
                         ege_number=ege_number,
                         default_answer_count=DEFAULT_ANSWER_COUNT,
                         special_format=SPECIAL_ANSWER_FORMAT)


@app.route('/api/upload-image', methods=['POST'])
def api_upload_image():
    """API для загрузки изображения (AJAX)"""
    if 'image' not in request.files:
        return jsonify({'error': 'Нет файла'}), 400
    
    image = request.files['image']
    if not image.filename:
        return jsonify({'error': 'Пустой файл'}), 400
    
    if not allowed_file(image.filename, ALLOWED_IMAGE_EXTENSIONS):
        return jsonify({'error': 'Только PNG файлы'}), 400
    
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
    variants = Variant.get_all()
    # Добавляем количество задач в каждом варианте
    for v in variants:
        v['tasks_count'] = len(Variant.get_tasks(v['id']))
    return render_template('teacher/variants.html', variants=variants)

@app.route('/variants/create', methods=['GET', 'POST'])
def variant_create():
    """Создание варианта"""
    if request.method == 'POST':
        name = request.form.get('name')
        variant_type = request.form.get('variant_type')
        generation_mode = request.form.get('generation_mode')
        
        if variant_type == 'thematic':
            ege_number = int(request.form.get('ege_number'))
            tasks_count = int(request.form.get('tasks_count', 10))
            
            # Получаем задачи для этого номера ЕГЭ
            available_tasks = Task.get_by_ege_number(ege_number)
            
            if len(available_tasks) < tasks_count:
                flash(f'Недостаточно задач для номера {ege_number}. Доступно: {len(available_tasks)}', 'error')
                return redirect(url_for('variant_create'))
            
            # Создаём вариант
            variant_id = Variant.create(name, 'thematic', ege_number)
            
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
            variant_id = Variant.create(name, 'mixed')
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
                return redirect(url_for('variant_create'))
            
            # Перемешиваем задачи
            random.shuffle(task_ids)
            Variant.add_tasks(variant_id, task_ids)
            
        else:  # full - полный вариант ЕГЭ
            variant_id = Variant.create(name, 'full')
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
        return redirect(url_for('variants_list'))
    
    # GET - показываем форму
    task_counts = Task.count_by_ege_number()
    all_tasks = {}
    for ege_num in range(1, 28):
        all_tasks[ege_num] = Task.get_by_ege_number(ege_num)
    
    from config import DEFAULT_ANSWER_COUNT
    
    return render_template('teacher/variant_create.html', 
                         task_counts=task_counts,
                         all_tasks=all_tasks,
                         default_answer_count=DEFAULT_ANSWER_COUNT)

@app.route('/variants/<int:variant_id>')
def variant_view(variant_id):
    """Просмотр варианта"""
    variant = Variant.get_by_id(variant_id)
    if not variant:
        flash('Вариант не найден', 'error')
        return redirect(url_for('variants_list'))
    
    tasks = Variant.get_tasks(variant_id)
    return render_template('teacher/variant_view.html', variant=variant, tasks=tasks)

@app.route('/variants/<int:variant_id>/delete', methods=['POST'])
def variant_delete(variant_id):
    """Удаление варианта"""
    variant = Variant.get_by_id(variant_id)
    if variant:
        deleted = Variant.delete(variant_id, cascade=True)
        if deleted:
            flash(f'Вариант "{variant["name"]}" удалён вместе с результатами', 'success')
    return redirect(url_for('variants_list'))


@app.route('/variants/upload', methods=['POST'])
def variant_upload():
    """Загрузка готового варианта с созданием задач в банке"""
    import uuid
    from config import DEFAULT_ANSWER_COUNT
    
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
        answer1 = request.form.get(f'answer1_{i}')
        answer2 = request.form.get(f'answer2_{i}')
        
        if not ege_number or not answer1:
            continue
        
        ege_number = int(ege_number)
        # Разрешаем ответы буквами/цифрами: сохраняем как строки
        answer1 = (answer1 or '').strip()
        answer2 = (answer2 or '').strip() or None
        
        # Определяем количество ответов
        answer_count = DEFAULT_ANSWER_COUNT.get(ege_number, 1)
        if answer2 is not None and answer2 != '':
            answer_count = 2
        
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
    sessions = TestSession.get_all()
    active_session = TestSession.get_active()
    
    # Добавляем информацию о студентах
    for s in sessions:
        students = TestSession.get_students(s['id'])
        s['students_count'] = len(students)
        s['finished_count'] = len([st for st in students if st['status'] == 'finished'])
        if s['variant_id']:
            variant = Variant.get_by_id(s['variant_id'])
            s['variant_name'] = variant['name'] if variant else 'Удалён'
        else:
            s['variant_name'] = 'Индивидуальные варианты'
    
    return render_template('teacher/sessions.html', 
                         sessions=sessions, 
                         active_session=active_session)

@app.route('/sessions/new', methods=['GET', 'POST'])
def session_new():
    """Создание нового тестирования"""
    if request.method == 'POST':
        variant_mode = request.form.get('variant_mode')
        time_limit = int(request.form.get('time_limit', 60))
        use_code = request.form.get('use_code') == '1'
        show_answers = request.form.get('show_answers') == '1'
        teacher_finish_only = request.form.get('teacher_finish_only') == '1'
        
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

@app.route('/sessions/<int:session_id>/monitor')
def session_monitor(session_id):
    """Мониторинг тестирования"""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('sessions_list'))
    
    students = TestSession.get_students(session_id)
    
    return render_template('teacher/session_monitor.html',
                         session=session,
                         students=students)


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
    return render_template('student/login.html', need_code=need_code, app_mode=app_mode)

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
    time_limit = test_session['time_limit'] if test_session else 60
    start_value = student.get('started_at')
    try:
        start_time = datetime.fromisoformat(start_value) if start_value else datetime.now()
    except Exception:
        start_time = datetime.now()
    elapsed = (datetime.now() - start_time).total_seconds()
    remaining = max(0, time_limit * 60 - elapsed)
    
    current_task = request.args.get('task', 0, type=int)
    
    return render_template('student/test.html',
                         student=student,
                         tasks=tasks,
                         answers=answers,
                         remaining=int(remaining),
                         current_task=current_task,
                         teacher_finish_only=bool(test_session and test_session.get('teacher_finish_only')),
                         app_mode=bool(session.get('app_mode')))

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

    if test_session['status'] == 'closed':
        if student['status'] != 'finished':
            Student.finish(student_id)
        return jsonify({'active': False, 'finished': True, 'redirect': url_for('student_result')})

    return jsonify({'active': True})

@app.route('/test/result')
def student_result():
    """Результат ученика"""
    from flask import session as flask_session
    
    student_id = flask_session.get('student_id')
    if not student_id:
        return redirect(url_for('student_login'))
    
    student = Student.get_by_id(student_id)
    test_session = TestSession.get_by_id(student['session_id'])

    if test_session and test_session.get('teacher_finish_only') and test_session['status'] != 'closed':
        return redirect(url_for('student_test'))

    if student and student['status'] != 'finished':
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
        'grade_5_min': test_session.get('grade_5_min'),
        'grade_4_min': test_session.get('grade_4_min'),
        'grade_3_min': test_session.get('grade_3_min')
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
    sessions = TestSession.get_all()
    
    for s in sessions:
        students = TestSession.get_students(s['id'])
        s['students_count'] = len(students)
        s['finished_count'] = len([st for st in students if st['status'] == 'finished'])
        
        # Критерии оценки для этой сессии
        session_criteria = {
            'grade_5_min': s.get('grade_5_min'),
            'grade_4_min': s.get('grade_4_min'),
            'grade_3_min': s.get('grade_3_min')
        }
        
        # Рассчитываем оценки для каждого ученика
        total_correct_all = 0
        for st in students:
            if st['status'] == 'finished':
                answers = Student.get_answers(st['id'])
                tasks = Variant.get_tasks(st['variant_id'])
                correct_count = len([a for a in answers if a['is_correct']])
                total_tasks = len(tasks)
                
                st['correct_count'] = correct_count
                st['total_tasks'] = total_tasks
                st['grade'] = GradeCriteria.calculate_grade(correct_count, total_tasks, session_criteria)
                
                total_correct_all += correct_count
            else:
                st['correct_count'] = 0
                st['total_tasks'] = 0
                st['grade'] = None
        
        s['students'] = students
        
        # Средний балл
        if s['finished_count'] > 0:
            s['avg_score'] = round(total_correct_all / s['finished_count'], 1)
        else:
            s['avg_score'] = 0
        
        if s['variant_id']:
            variant = Variant.get_by_id(s['variant_id'])
            s['variant_name'] = variant['name'] if variant else 'Удалён'
        else:
            s['variant_name'] = 'Индивидуальные варианты'
    
    return render_template('teacher/results.html', sessions=sessions)

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
        'grade_5_min': test_session.get('grade_5_min'),
        'grade_4_min': test_session.get('grade_4_min'),
        'grade_3_min': test_session.get('grade_3_min')
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
            st['started_at'][:16] if st['started_at'] else '',
            st['finished_at'][:16] if st['finished_at'] else '',
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
        
        if not file.filename.endswith('.zip'):
            flash('Файл должен быть ZIP-архивом', 'error')
            return redirect(url_for('tasks_import'))
        
        try:
            with zipfile.ZipFile(file, 'r') as zip_file:
                # Читаем манифест
                if 'manifest.json' not in zip_file.namelist():
                    flash('Неверный формат архива (нет manifest.json)', 'error')
                    return redirect(url_for('tasks_import'))
                
                manifest = json.loads(zip_file.read('manifest.json'))
                
                imported_count = 0
                skipped_count = 0
                
                for task_data in manifest['tasks']:
                    # Извлекаем изображение
                    image_filename = task_data['image_filename']
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
                        attach_zip_path = f"attachments/{task_data['attachment_path']}"
                        if attach_zip_path in zip_file.namelist():
                            attach_content = zip_file.read(attach_zip_path)
                            attach_save_path = os.path.join(DATA_DIR, 'attachments', task_data['attachment_path'])
                            
                            if os.path.exists(attach_save_path):
                                import uuid
                                ext = os.path.splitext(task_data['attachment_path'])[1]
                                new_name = f"{uuid.uuid4().hex}{ext}"
                                attach_save_path = os.path.join(DATA_DIR, 'attachments', new_name)
                                attachment_path = new_name
                            else:
                                attachment_path = task_data['attachment_path']
                            
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
