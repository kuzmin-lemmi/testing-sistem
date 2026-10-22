"""Учитель: банк задач, тестирования и мониторинг, проверка файлов, результаты, импорт и экспорт."""
import os
import re
from datetime import datetime, timezone

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
from werkzeug.utils import secure_filename

import catalog
import results_logic
from activity import log_event
from blueprints.common import (
    _calc_remaining_seconds,
    _get_session_display_name,
    _get_session_variant_options,
    _is_safe_stored_name,
    _pack_answers_for_task,
    _parse_dt,
    _read_answers_from_form,
    _session_title,
    _task_answers_list,
    _validate_attachment_file,
    _validate_upload,
    allowed_file,
    generate_unique_filename,
    grade_percents,
    show_access_code_option,
    to_local_dt,
)
from config import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    ALLOWED_IMAGE_EXTENSIONS,
    ATTACHMENTS_DIR,
    BACKUPS_DIR,
    DATA_DIR,
    DEFAULT_ANSWER_COUNT,
    IMAGES_DIR,
    MAX_IMAGE_SIZE,
    MAX_IMPORT_ZIP_SIZE,
    SPECIAL_ANSWER_FORMAT,
    STUDENT_UPLOADS_DIR,
)
from models import (
    Answer,
    ClassGroup,
    GradeCriteria,
    Student,
    Task,
    TestSession,
    Variant,
    backup_database,
    get_setting,
    set_setting,
)
from results_logic import format_away

bp = Blueprint('teacher', __name__)


# ==================== БАНК ЗАДАЧ ====================

@bp.route('/tasks')
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


@bp.route('/classes/add', methods=['POST'])
def class_add():
    """Добавление класса в справочник"""
    name = (request.form.get('class_name') or '').strip().upper()
    if not name:
        flash('Введите название класса (например, 7А)', 'error')
        return redirect(url_for('teacher.tasks_list', mode='class'))
    try:
        class_id = ClassGroup.create(name)
        flash(f'Класс {name} добавлен', 'success')
        return redirect(url_for('teacher.tasks_list', mode='class', class_id=class_id))
    except Exception:
        flash('Такой класс уже есть', 'warning')
        return redirect(url_for('teacher.tasks_list', mode='class'))


@bp.route('/tasks/move-to-class', methods=['POST'])
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
            return redirect(url_for('teacher.tasks_list', mode='class', class_id=origin_class_id))
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=ege_back))
    if not task_ids:
        flash('Выберите хотя бы одну задачу', 'warning')
        if origin_mode == 'class' and origin_class_id:
            return redirect(url_for('teacher.tasks_list', mode='class', class_id=origin_class_id))
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=ege_back))
    moved = Task.move_to_class(task_ids, class_id)
    flash(f'Перенесено задач: {moved}', 'success')
    if origin_mode == 'class' and origin_class_id:
        return redirect(url_for('teacher.tasks_list', mode='class', class_id=origin_class_id))
    return redirect(url_for('teacher.tasks_list', mode='ege', ege=ege_back))


@bp.route('/tasks/move-bank', methods=['POST'])
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
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

    if target == 'general':
        class_id = ClassGroup.get_default_id()
        Task.update(task_id, task_scope='class', class_id=class_id)
        flash(f'Задача #{task_id} перемещена в общий банк', 'success')
    elif target == 'ege':
        if not target_ege or target_ege < 1 or target_ege > 27:
            flash('Некорректный номер банка ЕГЭ', 'error')
            return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))
        Task.update(task_id, task_scope='ege', ege_number=target_ege, class_id=None)
        flash(f'Задача #{task_id} перемещена в ЕГЭ №{target_ege}', 'success')
    else:
        flash('Неизвестный целевой банк', 'error')

    return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))


@bp.route('/tasks/bulk-action', methods=['POST'])
def tasks_bulk_action():
    """Массовые операции: удаление или перенос в нужный банк"""
    action = (request.form.get('action') or '').strip().lower()
    task_ids = [int(tid) for tid in request.form.getlist('task_ids') if str(tid).isdigit()]
    return_ege = request.form.get('return_ege', type=int)
    if return_ege is None:
        return_ege = 1

    if not task_ids:
        flash('Отметьте хотя бы одну задачу', 'warning')
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

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
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

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
            return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

        if target == 'ege':
            if not target_ege or target_ege < 1 or target_ege > 27:
                flash('Выберите номер ЕГЭ для переноса', 'error')
                return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))
            for task_id in task_ids:
                Task.update(task_id, task_scope='ege', ege_number=target_ege, class_id=None)
                moved += 1
            flash(f'Перемещено в ЕГЭ №{target_ege}: {moved}', 'success')
            return redirect(url_for('teacher.tasks_list', mode='ege', ege=target_ege))

        flash('Выберите целевой банк для переноса', 'error')
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))

    flash('Неизвестное действие', 'error')
    return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))


@bp.route('/tasks/add', methods=['GET', 'POST'])
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
                return redirect(url_for('teacher.task_add', mode='class'))
            ege_number = 1
        else:
            ege_number = int(request.form.get('ege_number'))

        # Обработка изображения
        image = request.files.get('image')
        if not image or not image.filename:
            flash('Необходимо загрузить PNG-изображение', 'error')
            return redirect(url_for('teacher.task_add', mode=mode, ege=ege_number, class_id=class_id))
        valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
        if not valid_image:
            flash(image_error or 'Необходимо загрузить PNG-изображение', 'error')
            return redirect(url_for('teacher.task_add', mode=mode, ege=ege_number, class_id=class_id))

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
                return redirect(url_for('teacher.task_add', mode=mode, ege=ege_number, class_id=class_id))
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
                return redirect(url_for('teacher.task_add', mode=mode, ege=ege_number, class_id=class_id))

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
            return redirect(url_for('teacher.tasks_list', mode='class', class_id=class_id))
        flash(f'Задача для номера {ege_number} успешно добавлена', 'success')
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=ege_number))

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


@bp.route('/tasks/<int:task_id>')
def task_view(task_id):
    """Просмотр задачи"""
    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('teacher.tasks_list'))
    return render_template('teacher/task_view.html', task=task)


@bp.route('/tasks/<int:task_id>/edit', methods=['GET', 'POST'])
def task_edit(task_id):
    """Редактирование задачи"""
    task = Task.get_by_id(task_id)
    if not task:
        flash('Задача не найдена', 'error')
        return redirect(url_for('teacher.tasks_list'))

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
                return redirect(url_for('teacher.task_edit', task_id=task_id))
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
                return redirect(url_for('teacher.task_edit', task_id=task_id))

            updates['answer_count'] = answer_count
            updates['answer_1'], updates['answer_2'], updates['answer_text'] = _pack_answers_for_task(answer_values)

        # Обновление изображения (если загружено новое)
        image = request.files.get('image')
        if image and image.filename:
            valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
            if not valid_image:
                flash(image_error or 'Необходимо загрузить PNG-изображение', 'error')
                return redirect(url_for('teacher.task_edit', task_id=task_id))
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
                    return redirect(url_for('teacher.task_edit', task_id=task_id))

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
            return redirect(url_for('teacher.tasks_list', mode='class', class_id=updates.get('class_id')))
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=updates.get('ege_number', task['ege_number'])))

    return render_template('teacher/task_edit.html',
                          task=task,
                          classes=ClassGroup.get_all(),
                          existing_answers=_task_answers_list(task),
                          default_answer_count=DEFAULT_ANSWER_COUNT)


@bp.route('/tasks/<int:task_id>/delete', methods=['POST'])
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

    return redirect(url_for('teacher.tasks_list', mode='ege', ege=return_ege))


@bp.route('/tasks/bulk', methods=['GET', 'POST'])
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
                return redirect(url_for('teacher.tasks_list', mode='ege', ege=0))
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
            return redirect(url_for('teacher.tasks_list', mode='ege', ege=0))

        flash(f'Добавлено {created} задач для номера {ege_number}', 'success')
        return redirect(url_for('teacher.tasks_list', mode='ege', ege=ege_number))

    # GET - показываем форму
    ege_number = request.args.get('ege', type=int, default=1)
    return render_template('teacher/tasks_bulk.html',
                          ege_number=ege_number,
                          mode=mode,
                          class_id=class_id,
                          default_answer_count=DEFAULT_ANSWER_COUNT,
                          special_format=SPECIAL_ANSWER_FORMAT)


@bp.route('/api/upload-image', methods=['POST'])
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
        'url': url_for('system.serve_image', filename=image_filename)
    })


@bp.route('/api/delete-image', methods=['POST'])
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


# ==================== СКАЧИВАНИЕ И ПРОВЕРКА ФАЙЛОВ УЧЕНИКОВ (УЧИТЕЛЬ) ====================

@bp.route('/teacher/answers/download/<int:student_id>/<int:task_id>')
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


@bp.route('/teacher/answers/mark', methods=['POST'])
def teacher_mark_answer():
    """Учитель вручную ставит ✓ или ✗ на любом задании (например, ответ записан в другой форме)."""
    data = request.get_json(silent=True) or {}
    student_id = data.get('student_id')
    task_id = data.get('task_id')
    is_correct = data.get('is_correct')  # True / False

    if student_id is None or task_id is None or is_correct is None:
        return jsonify({'error': 'Неполные данные'}), 400

    Answer.mark(int(student_id), int(task_id), bool(is_correct))
    return jsonify({'success': True})


# ==================== API ====================

@bp.route('/api/tasks/count')
def api_tasks_count():
    """API: количество задач по номерам ЕГЭ"""
    return jsonify(Task.count_by_ege_number())


@bp.route('/api/tasks/<int:ege_number>')
def api_tasks_by_ege(ege_number):
    """API: задачи по номеру ЕГЭ"""
    tasks = Task.get_by_ege_number(ege_number)
    return jsonify(tasks)


# ==================== ТЕСТИРОВАНИЯ ====================

@bp.route('/sessions')
def sessions_list():
    """Тестирование: если идёт — сразу мониторинг, иначе запуск и последние прошедшие."""
    active_session = TestSession.get_active()
    if active_session:
        return redirect(url_for('teacher.session_monitor', session_id=active_session['id']))
    recent = [s for s in TestSession.get_all() if s['status'] == 'closed'][:5]
    for item in recent:
        item['title'] = _session_title(item)
        item['students_count'] = len(TestSession.get_students(item['id']))
    return render_template('teacher/sessions.html', recent=recent, variants_count=len(Variant.get_all()))


@bp.route('/sessions/delete/<int:session_id>', methods=['POST'])
def session_delete(session_id):
    """Удаление тестирования с результатами"""
    deleted = TestSession.delete_with_results(session_id)
    if deleted:
        flash('Тестирование удалено', 'success')
    else:
        flash('Тестирование не найдено', 'error')

    status = request.form.get('status', 'all')
    q = request.form.get('q', '')
    return redirect(url_for('teacher.sessions_list', status=status, q=q))


@bp.route('/sessions/delete-bulk', methods=['POST'])
def session_delete_bulk():
    """Массовое удаление тестирований"""
    session_ids = [int(sid) for sid in request.form.getlist('session_ids') if str(sid).isdigit()]
    status = request.form.get('status', 'all')
    q = request.form.get('q', '')

    if not session_ids:
        flash('Отметьте хотя бы одно тестирование для удаления', 'warning')
        return redirect(url_for('teacher.sessions_list', status=status, q=q))

    deleted_count = 0
    for session_id in session_ids:
        if TestSession.delete_with_results(session_id):
            deleted_count += 1

    flash(f'Удалено тестирований: {deleted_count}', 'success')
    return redirect(url_for('teacher.sessions_list', status=status, q=q))


@bp.route('/sessions/new', methods=['GET', 'POST'])
def session_new():
    """Создание нового тестирования"""
    active = TestSession.get_active()
    if active:
        flash('Сейчас уже идёт тестирование. Завершите его, прежде чем запускать новое.', 'error')
        return redirect(url_for('teacher.session_monitor', session_id=active['id']))
    if request.method == 'POST':
        variant_mode = request.form.get('variant_mode')
        time_limit = max(1, min(300, request.form.get('time_limit', 45, type=int) or 45))
        use_code = request.form.get('use_code') == '1'
        show_answers = request.form.get('show_answers') == '1'
        teacher_finish_only = True  # ученик не завершает сам (иначе его ответы увидит сосед); всё открывается после завершения учителем
        calculator_enabled = request.form.get('calculator_enabled') == '1'
        python_enabled = request.form.get('python_enabled') == '1'
        shuffle_tasks = request.form.get('shuffle_tasks') == '1'
        mask_answers = request.form.get('mask_answers') == '1'
        set_setting('last_mask', '1' if mask_answers else '0')

        set_setting('last_shuffle', '1' if shuffle_tasks else '0')
        set_setting('last_time_limit', time_limit)
        set_setting('last_calculator', '1' if calculator_enabled else '0')
        set_setting('last_python', '1' if python_enabled else '0')

        # Критерии оценки: число заданий берём из самого варианта, пороги — из формы или из процентов
        if variant_mode == 'single' and request.form.get('variant_id', type=int):
            total_tasks = len(Variant.get_tasks(request.form.get('variant_id', type=int)))
        elif variant_mode == 'multi_choice' and request.form.getlist('variant_ids'):
            total_tasks = len(Variant.get_tasks(int(request.form.getlist('variant_ids')[0])))
        else:
            total_tasks = request.form.get('total_tasks', 27, type=int) or 27
        total_tasks = max(1, total_tasks)
        defaults = results_logic.grade_minimums(total_tasks, grade_percents())
        grade_5_min, grade_4_min, grade_3_min = (
            max(1, min(total_tasks, request.form.get(f'grade_{g}_min', d, type=int) or d))
            for g, d in zip((5, 4, 3), defaults))
        grade_4_min = min(grade_4_min, grade_5_min)
        grade_3_min = min(grade_3_min, grade_4_min)

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
                return redirect(url_for('teacher.session_new'))

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
                shuffle_tasks=shuffle_tasks,
                mask_answers=mask_answers,
                grade_5_min=grade_5_min,
                grade_4_min=grade_4_min,
                grade_3_min=grade_3_min,
                total_tasks=total_tasks
            )
        elif variant_mode == 'multi_choice':
            variant_ids = [int(v) for v in request.form.getlist('variant_ids') if str(v).isdigit()]
            if len(variant_ids) < 2:
                flash('Выберите минимум два готовых варианта', 'error')
                return redirect(url_for('teacher.session_new'))

            task_counts = [len(Variant.get_tasks(variant_id)) for variant_id in variant_ids]
            if len(set(task_counts)) > 1:
                flash('Для режима выбора учениками все варианты должны содержать одинаковое количество задач', 'error')
                return redirect(url_for('teacher.session_new'))

            session_id = TestSession.create(
                variant_id=None,
                individual_mode=False,
                time_limit=time_limit,
                access_code=access_code,
                show_answers=show_answers,
                teacher_finish_only=teacher_finish_only,
                calculator_enabled=calculator_enabled,
                python_enabled=python_enabled,
                shuffle_tasks=shuffle_tasks,
                mask_answers=mask_answers,
                grade_5_min=grade_5_min,
                grade_4_min=grade_4_min,
                grade_3_min=grade_3_min,
                total_tasks=total_tasks
            )
            TestSession.set_variants(session_id, variant_ids)
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
                    shuffle_tasks=shuffle_tasks,
                    mask_answers=mask_answers,
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
                    shuffle_tasks=shuffle_tasks,
                    mask_answers=mask_answers,
                    thematic_ege_number=ege_number,
                    thematic_tasks_count=tasks_count,
                    grade_5_min=grade_5_min,
                    grade_4_min=grade_4_min,
                    grade_3_min=grade_3_min,
                    total_tasks=total_tasks
                )

        started = TestSession.get_by_id(session_id)
        log_event(f"Запущено тестирование «{_get_session_display_name(started)}» на {time_limit} мин.", 'ok')
        flash('Тестирование запущено. Ученики увидят форму входа через несколько секунд.', 'success')
        return redirect(url_for('teacher.session_monitor', session_id=session_id))

    # GET - показываем форму
    variants = [v for v in Variant.get_all() if not (v['name'] or '').startswith('Индивидуальный_')]
    for v in variants:
        v['tasks_count'] = len(Variant.get_tasks(v['id']))
    variants = [v for v in variants if v['tasks_count']]
    task_counts = Task.count_by_ege_number()
    preselected_variant = request.args.get('variant_id', type=int)
    saved_criteria = GradeCriteria.get_all()

    # «Запустить комплект» из каталога: все варианты комплекта отмечены, время подставлено
    preset_kit, preset_ids, preset_minutes = request.args.get('kit', ''), [], None
    if preset_kit:
        grade, topic = request.args.get('kit_grade', ''), request.args.get('kit_topic', '')
        kit_variants = [v for v in variants if (v.get('kit') or '') == preset_kit
                        and (v.get('grade') or '') == grade and (v.get('topic') or '') == topic]
        kit_variants.sort(key=lambda v: (len(v['name'] or ''), (v['name'] or '').casefold()))
        preset_ids = [v['id'] for v in kit_variants]
        preset_minutes = next((v['minutes'] for v in kit_variants if v.get('minutes')), None)
        if len(preset_ids) == 1:
            preselected_variant = preset_ids[0]
    elif preselected_variant:
        chosen = next((v for v in variants if v['id'] == preselected_variant), None)
        preset_minutes = chosen.get('minutes') if chosen else None
    if len(preset_ids) > 1:
        # варианты комплекта — первыми, в порядке номеров: ученик видит «Вариант 1, 2, 3…»
        order = {vid: i for i, vid in enumerate(preset_ids)}
        variants.sort(key=lambda v: order.get(v['id'], len(order)))

    works = catalog.build_works(variants)
    selected_ids = set(preset_ids) if len(preset_ids) > 1 else {preselected_variant}
    if len(preset_ids) > 1 or preselected_variant:   # выбранная работа — первой в списке
        works.sort(key=lambda w: not any(v['id'] in selected_ids for v in w['variants']))
    return render_template('teacher/session_new.html',
                         variants=variants,
                         works=works,
                         filter_grades=sorted({w['grade'] for w in works}, key=catalog._grade_order),
                         grade_short=catalog.grade_short,
                         task_counts=task_counts,
                         preselected_variant=preselected_variant,
                          preset_kit=preset_kit if len(preset_ids) > 1 else '',
                          preset_ids=preset_ids if len(preset_ids) > 1 else [],
                          show_access_code_option=show_access_code_option(),
                          percents=grade_percents(),
                          last={'time_limit': preset_minutes or get_setting('last_time_limit', '45'),
                                'calculator': get_setting('last_calculator') == '1',
                                'python': get_setting('last_python') == '1',
                                'shuffle': get_setting('last_shuffle') == '1',
                                'mask': get_setting('last_mask') == '1'},
                          saved_criteria=saved_criteria)


@bp.route('/sessions/<int:session_id>/close', methods=['POST'])
def session_close(session_id):
    """Завершить тестирование: ученики сразу видят оценки, учитель — результаты."""
    Student.finish_all(session_id)
    TestSession.close(session_id)
    log_event('Тестирование завершено, результаты сохранены.', 'ok')
    try:
        backup_database(BACKUPS_DIR)  # копия базы после каждого проведённого теста
    except Exception:
        current_app.logger.exception('Не удалось сделать копию базы после теста')
    flash('Тестирование завершено. Ученики видят свои оценки и правильные ответы.', 'success')
    return redirect(url_for('teacher.results_list', session_id=session_id))


@bp.route('/sessions/<int:session_id>/pause', methods=['POST'])
def session_pause(session_id):
    """Поставить тестирование на паузу"""
    session = TestSession.get_by_id(session_id)
    if session and session['status'] == 'active':
        TestSession.pause(session_id)
        log_event('Тестирование поставлено на паузу.', 'warn')
        flash('Тестирование поставлено на паузу', 'success')
    return redirect(url_for('teacher.session_monitor', session_id=session_id))


@bp.route('/sessions/<int:session_id>/resume', methods=['POST'])
def session_resume(session_id):
    """Продолжить тестирование"""
    session = TestSession.get_by_id(session_id)
    if session and session['status'] == 'active':
        TestSession.resume(session_id)
        log_event('Тестирование продолжено.', 'info')
        flash('Тестирование продолжено', 'success')
    return redirect(url_for('teacher.session_monitor', session_id=session_id))


@bp.route('/sessions/<int:session_id>/extend', methods=['POST'])
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
            log_event(f'Время тестирования продлено на {minutes_int} мин.', 'info')
            flash(f'Время продлено на {minutes_int} мин', 'success')
    return redirect(url_for('teacher.session_monitor', session_id=session_id))


@bp.route('/sessions/<int:session_id>/monitor')
def session_monitor(session_id):
    """Мониторинг: таймер, кто в сети, сколько ответил. Данные обновляются сами (/live)."""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('teacher.sessions_list'))
    if session['status'] == 'closed':
        return redirect(url_for('teacher.results_list', session_id=session_id))
    return render_template('teacher/session_monitor.html', session=session, title=_get_session_display_name(session),
                           live=_monitor_state(session),
                           session_variants=_get_session_variant_options(session_id, include_task_counts=True))


ONLINE_SECONDS = 25  # окно ученика отмечается каждые 5 секунд


def _monitor_state(session):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    students = []
    tasks_cache = {}
    for st in TestSession.get_students(session['id']):
        tasks = tasks_cache.setdefault(st['variant_id'], Variant.get_tasks(st['variant_id']))
        score = results_logic.student_score(st, session, tasks)
        last_seen = _parse_dt(st.get('last_seen_at'))
        online = bool(last_seen and (now - last_seen).total_seconds() <= ONLINE_SECONDS)
        remaining = _calc_remaining_seconds(st, session)
        students.append({
            'id': st['id'],
            'name': f"{st['last_name']} {st['first_name']}",
            'variant': st.get('variant_label'),
            'online': online,
            'answered': score['answered'],
            'total': score['total'],
            'remaining': remaining,
            'started': to_local_dt(st.get('started_at'))[11:16],
            'last_seen': to_local_dt(st.get('last_seen_at'))[11:16] if st.get('last_seen_at') else '',
            'uploads': Answer.count_uploads_for_student(st['id']),
            'all_answered': bool(score['total']) and score['answered'] >= score['total'],
            'submitted': to_local_dt(st.get('submitted_at'))[11:16] if st.get('submitted_at') else '',
            # в сети, но страница теста скрыта или не в фокусе (другое окно, вкладка, свёрнут браузер)
            'away': online and bool(st.get('left_at')),
            'leaves': int(st.get('leave_count') or 0),
            'away_text': format_away(st),
            'in_app': bool(st.get('in_app')),
            'finished': st.get('status') == 'finished',
            'foreign_app': st.get('foreign_app') or '',
        })
    students.sort(key=lambda s: s['name'].casefold())
    in_progress = [s['remaining'] for s in students]
    full = session['time_limit'] * 60 + (session.get('extra_seconds') or 0)
    return {
        'status': session['status'],
        'paused': bool(session.get('paused')),
        # Пока никто не вошёл, время не идёт: у каждого ученика отсчёт начинается с его входа.
        'waiting': not in_progress,
        'remaining': max(in_progress) if in_progress else full,
        'students': students,
        'online': sum(1 for s in students if s['online']),
        'away': sum(1 for s in students if s['away']),
        'finished_all': bool(students) and all(s['remaining'] <= 0 or s['submitted'] or s['finished'] for s in students),
        'submitted_count': sum(1 for s in students if s['submitted']),
        'ready_count': sum(1 for s in students if s['all_answered'] and not s['submitted'] and not s['finished']),
    }


def _session_student(session_id, student_id):
    student = Student.get_by_id(student_id)
    if not student or student['session_id'] != session_id or student['status'] != 'in_progress':
        return None
    return student


@bp.route('/sessions/<int:session_id>/students/<int:student_id>/accept', methods=['POST'])
def session_accept_work(session_id, student_id):
    """Учитель принимает работу: у ученика заставка, ответы больше не меняются."""
    student = _session_student(session_id, student_id)
    if student and not student.get('submitted_at'):
        Student.set_submitted(student_id, True)
        log_event(f"Учитель принял работу: {student['last_name']} {student['first_name']}.", 'info')
    return jsonify({'ok': bool(student)})


@bp.route('/sessions/<int:session_id>/students/<int:student_id>/return', methods=['POST'])
def session_return_work(session_id, student_id):
    """Вернуть ученика к решению (если сдал случайно)."""
    student = _session_student(session_id, student_id)
    if student and student.get('submitted_at'):
        Student.set_submitted(student_id, False)
        log_event(f"{student['last_name']} {student['first_name']} возвращён к решению.", 'warn')
    return jsonify({'ok': bool(student)})


@bp.route('/sessions/<int:session_id>/students/<int:student_id>/finish', methods=['POST'])
def session_finish_student(session_id, student_id):
    """Учитель завершает тест одному ученику: у него сразу оценка, войти снова и менять ответы нельзя.
    Остальные продолжают решать."""
    student = _session_student(session_id, student_id)
    if student:
        Student.finish(student_id)
        log_event(f"Учитель завершил тест ученику: {student['last_name']} {student['first_name']}.", 'warn')
    return jsonify({'ok': bool(student)})


@bp.route('/sessions/<int:session_id>/accept-ready', methods=['POST'])
def session_accept_ready(session_id):
    """Принять работы у всех, кто ответил на все задания."""
    session_data = TestSession.get_by_id(session_id)
    if not session_data:
        return jsonify({'ok': False}), 404
    accepted = 0
    for item in _monitor_state(session_data)['students']:
        if item['all_answered'] and not item['submitted']:
            student = _session_student(session_id, item['id'])
            if student:
                Student.set_submitted(item['id'], True)
                accepted += 1
    if accepted:
        log_event(f'Учитель принял работы у всех, кто ответил на всё: {accepted}.', 'info')
    return jsonify({'ok': True, 'accepted': accepted})


@bp.route('/sessions/<int:session_id>/live')
def session_live(session_id):
    session = TestSession.get_by_id(session_id)
    if not session:
        return jsonify({'error': 'not found'}), 404
    return jsonify(_monitor_state(session))


# ==================== РЕЗУЛЬТАТЫ ====================

@bp.route('/results')
def results_list():
    """Результаты: слева прошедшие тестирования, справа таблица «ученик × задание»."""
    search_query = (request.args.get('q', '') or '').strip().lower()
    selected_session_id = request.args.get('session_id', type=int)

    sessions = []
    for s in TestSession.get_all():
        s['title'] = _session_title(s)
        if search_query and search_query not in s['title'].lower():
            continue
        s['students_count'] = len(TestSession.get_students(s['id']))
        sessions.append(s)

    selected_session = None
    if sessions:
        ids = {s['id'] for s in sessions}
        if not selected_session_id or selected_session_id not in ids:
            selected_session_id = sessions[0]['id']
        selected_session = next(s for s in sessions if s['id'] == selected_session_id)

    groups, summary = results_logic.session_results(selected_session) if selected_session else ([], None)
    return render_template('teacher/results.html', sessions=sessions, selected_session=selected_session,
                           selected_session_id=selected_session_id, groups=groups, summary=summary,
                           search_query=search_query)


@bp.route('/results/session/<int:session_id>')
def result_session(session_id):
    """Старый адрес: результаты тестирования теперь на общей странице «Результаты»."""
    return redirect(url_for('teacher.results_list', session_id=session_id))


@bp.route('/results/student/<int:student_id>')
def result_student(student_id):
    """Ответы ученика по заданиям; учитель может вручную поставить «верно» или «неверно»."""
    student = Student.get_by_id(student_id)
    if not student:
        flash('Ученик не найден', 'error')
        return redirect(url_for('teacher.results_list'))
    test_session = TestSession.get_by_id(student['session_id'])
    score = results_logic.student_score(student, test_session)
    matches = []
    if test_session:
        _, summary = results_logic.session_results(test_session)
        matches = [m for m in summary['matches'] if student_id in (m['students'][0]['id'], m['students'][1]['id'])]
    return render_template('teacher/result_student.html', student=student, test_session=test_session,
                           session_title=_session_title(test_session) if test_session else '', score=score,
                           matches=matches)


@bp.route('/results/export/<int:session_id>.xlsx')
def result_export_xlsx(session_id):
    """Excel: лист «Результаты» (ученик × задание, цветом) и лист «Правильные ответы»."""
    test_session = TestSession.get_by_id(session_id)
    if not test_session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('teacher.results_list'))
    groups, _ = results_logic.session_results(test_session)
    title = _get_session_display_name(test_session)
    subtitle = f"Тестирование: {to_local_dt(test_session.get('created_at'))}" + \
               ('' if test_session['status'] == 'closed' else ' (ещё идёт)')
    data = results_logic.build_workbook(title, subtitle, groups)
    stamp = to_local_dt(test_session.get('created_at')).replace(':', '-').replace(' ', '_')
    safe = re.sub(r'[\\/:*?"<>|]', ' ', title).strip() or 'Результаты'
    import io
    return send_file(io.BytesIO(data), as_attachment=True, download_name=f'{safe} {stamp}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/results/export/<int:session_id>')
def result_export(session_id):
    """Экспорт результатов в CSV"""
    import csv
    from io import StringIO

    from flask import Response

    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('teacher.results_list'))

    students = TestSession.get_students(session_id)
    session_criteria = {
        'grade_5_min': session.get('grade_5_min'),
        'grade_4_min': session.get('grade_4_min'),
        'grade_3_min': session.get('grade_3_min')
    }

    def _format_answer(task, answer):
        if not answer:
            return ''
        if task.get('answer_kind') == 'file_upload':
            return answer.get('upload_name') or 'Файл не загружен'

        values = []
        if answer.get('answer_text') not in (None, ''):
            values.append(str(answer['answer_text']))
        else:
            if answer.get('answer_1') is not None:
                values.append(str(answer['answer_1']))
            if answer.get('answer_2') is not None:
                values.append(str(answer['answer_2']))
        return ', '.join(values)

    def _format_status(task, answer):
        if not answer:
            return ''
        if task.get('answer_kind') == 'file_upload' and answer.get('upload_name') and answer.get('is_correct') is None:
            return '🟡 Не проверено'
        return '🟢 Верно' if answer.get('is_correct') else '🔴 Неверно'

    students_export = []
    max_tasks = 0
    for st in students:
        tasks = Variant.get_tasks(st['variant_id'])
        answers = Student.get_answers(st['id'])
        answers_dict = {a['task_id']: a for a in answers}
        correct = len([a for a in answers if a['is_correct']])
        total = len(tasks)
        grade = GradeCriteria.calculate_grade(correct, total, session_criteria)

        task_cells = []
        for task in tasks:
            ans = answers_dict.get(task['id'])
            task_cells.extend([
                f"№{task['ege_number']}" if task.get('task_scope', 'ege') == 'ege' else '',
                _format_answer(task, ans),
                _format_status(task, ans),
            ])

        max_tasks = max(max_tasks, len(tasks))
        students_export.append({
            'student': st,
            'correct': correct,
            'total': total,
            'grade': grade,
            'task_cells': task_cells,
        })

    # Создаём CSV
    output = StringIO()
    writer = csv.writer(output, delimiter=';')

    # Заголовок
    header = ['Фамилия', 'Имя', 'Вариант', 'Начало', 'Завершение', 'Правильных', 'Всего', 'Оценка']
    for i in range(1, max_tasks + 1):
        header.extend([
            f'Задание {i} (№ ЕГЭ)',
            f'Задание {i} (ответ)',
            f'Задание {i} (статус)'
        ])
    writer.writerow(header)

    for item in students_export:
        st = item['student']
        row = [
            st['last_name'],
            st['first_name'],
            st.get('variant_label') or '',
            to_local_dt(st['started_at']) if st['started_at'] else '',
            to_local_dt(st['finished_at']) if st['finished_at'] else '',
            item['correct'],
            item['total'],
            item['grade']
        ]
        row.extend(item['task_cells'])
        missing_cells = (max_tasks * 3) - len(item['task_cells'])
        if missing_cells > 0:
            row.extend([''] * missing_cells)
        writer.writerow([
            *row
        ])

    csv_text = '\ufeff' + output.getvalue()

    return Response(
        csv_text,
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=results_{session_id}.csv'}
    )


@bp.route('/results/delete/<int:session_id>', methods=['POST'])
def result_delete(session_id):
    """Удаление результатов тестирования"""
    session = TestSession.get_by_id(session_id)
    if not session:
        flash('Тестирование не найдено', 'error')
        return redirect(url_for('teacher.results_list'))

    deleted = TestSession.delete_with_results(session_id)
    if deleted:
        flash('Результаты тестирования удалены', 'success')
    else:
        flash('Не удалось удалить результаты', 'error')

    period = request.form.get('period', 'all')
    q = request.form.get('q', '')
    return redirect(url_for('teacher.results_list', period=period, q=q))


@bp.route('/results/delete-bulk', methods=['POST'])
def result_delete_bulk():
    """Массовое удаление результатов тестирований"""
    session_ids = [int(sid) for sid in request.form.getlist('session_ids') if str(sid).isdigit()]
    period = request.form.get('period', 'all')
    q = request.form.get('q', '')

    if not session_ids:
        flash('Отметьте хотя бы одно тестирование для удаления', 'warning')
        return redirect(url_for('teacher.results_list', period=period, q=q))

    deleted_count = 0
    for session_id in session_ids:
        if TestSession.delete_with_results(session_id):
            deleted_count += 1

    if deleted_count:
        flash(f'Удалено тестирований: {deleted_count}', 'success')
    else:
        flash('Не удалось удалить выбранные тестирования', 'error')

    return redirect(url_for('teacher.results_list', period=period, q=q))


# ==================== ИМПОРТ/ЭКСПОРТ ====================

@bp.route('/tasks/export')
def tasks_export():
    """Экспорт банка задач в ZIP"""
    import io
    import json
    import zipfile

    # Получаем все задачи
    all_tasks = []
    for ege_num in range(1, 28):
        tasks = Task.get_by_ege_number(ege_num)
        all_tasks.extend(tasks)

    if not all_tasks:
        flash('Нет задач для экспорта', 'error')
        return redirect(url_for('teacher.tasks_list'))

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


@bp.route('/tasks/import', methods=['GET', 'POST'])
def tasks_import():
    """Импорт банка задач из ZIP"""
    import json
    import zipfile

    if request.method == 'POST':
        if 'file' not in request.files:
            flash('Файл не выбран', 'error')
            return redirect(url_for('teacher.tasks_import'))

        file = request.files['file']
        if file.filename == '':
            flash('Файл не выбран', 'error')
            return redirect(url_for('teacher.tasks_import'))

        valid_zip, zip_error = _validate_upload(file, MAX_IMPORT_ZIP_SIZE, {'zip'}, 'zip')
        if not valid_zip:
            flash(zip_error or 'Файл должен быть ZIP-архивом', 'error')
            return redirect(url_for('teacher.tasks_import'))

        try:
            with zipfile.ZipFile(file, 'r') as zip_file:
                infos = zip_file.infolist()
                if len(infos) > 2000:
                    flash('Слишком много файлов в архиве', 'error')
                    return redirect(url_for('teacher.tasks_import'))

                total_uncompressed = sum(i.file_size for i in infos)
                if total_uncompressed > 200 * 1024 * 1024:
                    flash('Архив слишком большой после распаковки', 'error')
                    return redirect(url_for('teacher.tasks_import'))

                # Читаем манифест
                if 'manifest.json' not in zip_file.namelist():
                    flash('Неверный формат архива (нет manifest.json)', 'error')
                    return redirect(url_for('teacher.tasks_import'))

                manifest = json.loads(zip_file.read('manifest.json'))
                if not isinstance(manifest, dict) or not isinstance(manifest.get('tasks'), list):
                    flash('Неверный формат manifest.json', 'error')
                    return redirect(url_for('teacher.tasks_import'))

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
                return redirect(url_for('teacher.tasks_list'))

        except Exception as e:
            flash(f'Ошибка при импорте: {str(e)}', 'error')
            return redirect(url_for('teacher.tasks_import'))

    return render_template('teacher/tasks_import.html')
