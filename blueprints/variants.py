"""Варианты: список, создание, конструктор, загрузка из LocalShot и из папки."""
import hashlib
import io
import os
import uuid
from datetime import datetime

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for

import catalog
import variant_folder
from blueprints.common import (
    _error_response,
    _pack_answers_for_task,
    _read_answers_from_form,
    _validate_attachment_file,
    _validate_upload,
    generate_unique_filename,
    show_ege_banks,
)
from config import (
    ALLOWED_ATTACHMENT_EXTENSIONS,
    ALLOWED_IMAGE_EXTENSIONS,
    ATTACHMENTS_DIR,
    DATA_DIR,
    IMAGES_DIR,
    MAX_ATTACHMENT_SIZE,
    MAX_IMAGE_SIZE,
    MAX_LOCALSHOT_FILE_SIZE,
    MAX_LOCALSHOT_IMAGE_SIZE,
    MAX_LOCALSHOT_TASKS,
)
from localshot_import import MAX_ANSWERS as MAX_TASK_ANSWERS
from localshot_import import (
    MODE_FIELDS,
    MODE_MULTILINE,
    ImportedTask,
    VariantFileError,
    check_attachment,
    parse_variant_file,
)
from localshot_import import clean_name as clean_variant_name
from models import (
    ClassGroup,
    Task,
    Variant,
    get_setting,
    known_variant_files,
    recheck_task_answers,
    remember_variant_file,
    remove_task_from_variant,
    variant_usage_counts,
)
from results_logic import tasks_word

bp = Blueprint('variants', __name__)


# ==================== ВАРИАНТЫ ====================

@bp.route('/variants')
def variants_list():
    """Список вариантов"""
    scope = request.args.get('scope', 'all')
    selected_variant_id = request.args.get('variant_id', type=int)
    variants = Variant.get_all(None if scope == 'all' else scope)
    if not show_ege_banks():
        variants = [v for v in variants if not (v['name'] or '').startswith('Индивидуальный_')]
    usage = variant_usage_counts()
    for v in variants:
        v['used_count'] = usage.get(v['id'], 0)
    classes_by_id = {c['id']: c for c in ClassGroup.get_all()}
    # Добавляем количество задач в каждом варианте
    for v in variants:
        v['tasks_count'] = len(Variant.get_tasks(v['id']))
        if v.get('variant_scope') == 'class' and v.get('class_id'):
            group = classes_by_id.get(v['class_id'])
            v['class_name'] = group['name'] if group else f"#{v['class_id']}"

    stats = Variant.results_stats()
    for v in variants:
        v['stats'] = stats.get(v['id'])

    works = catalog.build_works(variants)
    selected_variant = None
    selected_work = None
    selected_tasks = []
    if variants:
        ids = {v['id'] for v in variants}
        if not selected_variant_id or selected_variant_id not in ids:
            selected_variant_id = works[0]['variants'][0]['id']
        selected_work = next(w for w in works if any(v['id'] == selected_variant_id for v in w['variants']))
        selected_variant = next((v for v in variants if v['id'] == selected_variant_id), None)
        if selected_variant:
            selected_tasks = Variant.get_tasks(selected_variant_id)

    def known(field):
        return sorted({v.get(field) for v in variants if v.get(field)}, key=str.casefold)

    grades = sorted({w['grade'] for w in works}, key=catalog._grade_order)
    bank_dir = variant_folder.folder(get_setting)
    bank_new = (variant_folder.new_files(bank_dir, known_variant_files(), {v['name'] for v in Variant.get_all()})
                if bank_dir and os.path.isdir(bank_dir) else [])
    return render_template('teacher/variants.html',
                          variants=variants,
                          works=works,
                          filter_grades=grades,
                          filter_topics=sorted({w['topic'] for w in works if w['topic']}, key=str.casefold),
                          grade_short=catalog.grade_short,
                          bank_dir=bank_dir, bank_exists=bool(bank_dir) and os.path.isdir(bank_dir),
                          bank_new=[name for _, _, name in bank_new[:8]], bank_new_count=len(bank_new),
                          selected_work=selected_work,
                          known_grades=sorted(set(known('grade')) | {f'{n} класс' for n in range(5, 12)},
                                              key=lambda g: (not g[:2].strip().isdigit(), int(g.split()[0]) if g.split()[0].isdigit() else 0, g)),
                          known_topics=known('topic'),
                          known_kits=known('kit'),
                          scope=scope,
                          selected_variant=selected_variant,
                          selected_variant_id=selected_variant_id,
                          selected_tasks=selected_tasks)


@bp.route('/variants/<int:variant_id>/catalog', methods=['POST'])
def variant_set_catalog(variant_id):
    """Место варианта в каталоге: из карточки варианта (форма) или перетаскиванием (JSON)."""
    if not Variant.get_by_id(variant_id):
        return _error_response('Вариант не найден', 404)
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    Variant.set_catalog(variant_id,
                        grade=data.get('grade') if 'grade' in data else None,
                        topic=data.get('topic') if 'topic' in data else None,
                        kit=data.get('kit') if 'kit' in data else None,
                        minutes=(data.get('minutes') or '') if 'minutes' in data else None)
    if request.is_json:
        return jsonify({'ok': True})
    flash('Место в каталоге сохранено', 'success')
    return redirect(url_for('variants.variants_list', variant_id=variant_id))


@bp.route('/variants/create', methods=['GET', 'POST'])
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
                return redirect(url_for('variants.variant_create', scope='class'))
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
            return redirect(url_for('variants.variants_list', scope='class'))

        if variant_type == 'thematic':
            ege_number = int(request.form.get('ege_number'))
            tasks_count = int(request.form.get('tasks_count', 10))

            # Получаем задачи для этого номера ЕГЭ
            available_tasks = Task.get_by_ege_number(ege_number)

            if len(available_tasks) < tasks_count:
                flash(f'Недостаточно задач для номера {ege_number}. Доступно: {len(available_tasks)}', 'error')
                return redirect(url_for('variants.variant_create', scope='ege'))

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
                return redirect(url_for('variants.variant_create', scope='ege'))

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
        return redirect(url_for('variants.variants_list', scope='ege'))

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


@bp.route('/variants/<int:variant_id>')
def variant_view(variant_id):
    """Вариант: задания с картинками, правка ответов, порядок, переименование."""
    variant = Variant.get_by_id(variant_id)
    if not variant:
        flash('Вариант не найден', 'error')
        return redirect(url_for('variants.variants_list'))
    tasks = Variant.get_tasks(variant_id)
    return render_template('teacher/variant_view.html', variant=variant, tasks=tasks,
                           used_count=variant_usage_counts().get(variant_id, 0))


def _variant_task_or_404(variant_id, task_id):
    if task_id not in {t['id'] for t in Variant.get_tasks(variant_id)}:
        flash('Задание не найдено в этом варианте', 'error')
        return None
    return Task.get_by_id(task_id)


@bp.route('/variants/<int:variant_id>/rename', methods=['POST'])
def variant_rename(variant_id):
    name = ' '.join((request.form.get('name') or '').split())[:80]
    if not name:
        flash('Название не может быть пустым', 'error')
    elif Variant.get_by_id(variant_id):
        Variant.update(variant_id, name=name)
        flash(f'Вариант переименован: «{name}»', 'success')
    return redirect(request.referrer or url_for('variants.variant_view', variant_id=variant_id))


@bp.route('/variants/<int:variant_id>/tasks/<int:task_id>/answer', methods=['POST'])
def variant_task_answer(variant_id, task_id):
    """Исправить правильный ответ; уже сохранённые ответы учеников перепроверяются."""
    task = _variant_task_or_404(variant_id, task_id)
    if task is None or task.get('answer_kind') == 'file_upload':
        return redirect(url_for('variants.variant_view', variant_id=variant_id))
    if int(task.get('answer_count') or 0) == 0:
        text = (request.form.get('answer_text') or '').strip()
        if not text:
            flash('Ответ не может быть пустым', 'error')
            return redirect(url_for('variants.variant_view', variant_id=variant_id))
        Task.update(task_id, answer_text=text)
    else:
        values = [v.strip() for v in request.form.getlist('answer')][:int(task['answer_count'])]
        if len(values) != int(task['answer_count']) or any(not v for v in values):
            flash('Заполните все поля ответа', 'error')
            return redirect(url_for('variants.variant_view', variant_id=variant_id))
        answer_1, answer_2, answer_text = _pack_answers_for_task(values)
        Task.update(task_id, answer_1=answer_1, answer_2=answer_2, answer_text=answer_text)
    rechecked = recheck_task_answers(task_id)
    flash('Ответ исправлен' + (f'; перепроверено ответов учеников: {rechecked}' if rechecked else ''), 'success')
    return redirect(url_for('variants.variant_view', variant_id=variant_id) + f'#task-{task_id}')


@bp.route('/variants/<int:variant_id>/tasks/<int:task_id>/remove', methods=['POST'])
def variant_task_remove(variant_id, task_id):
    if _variant_task_or_404(variant_id, task_id) is not None:
        remove_task_from_variant(variant_id, task_id)
        flash('Задание убрано из варианта', 'success')
    return redirect(url_for('variants.variant_view', variant_id=variant_id))


@bp.route('/variants/<int:variant_id>/tasks/<int:task_id>/move', methods=['POST'])
def variant_task_move(variant_id, task_id):
    ids = [t['id'] for t in Variant.get_tasks(variant_id)]
    if task_id in ids:
        index = ids.index(task_id)
        target = index + (-1 if request.form.get('direction') == 'up' else 1)
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            Variant.replace_tasks(variant_id, ids)
    return redirect(url_for('variants.variant_view', variant_id=variant_id) + f'#task-{task_id}')


@bp.route('/variants/<int:variant_id>/edit', methods=['GET', 'POST'])
def variant_edit(variant_id):
    """Редактирование варианта и состава задач"""
    variant = Variant.get_by_id(variant_id)
    if not variant:
        flash('Вариант не найден', 'error')
        return redirect(url_for('variants.variants_list'))

    current_tasks = Variant.get_tasks(variant_id)

    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        if not name:
            flash('Название варианта не может быть пустым', 'error')
            return redirect(url_for('variants.variant_edit', variant_id=variant_id))

        remove_ids = request.form.getlist('remove_task_ids')
        add_ids = request.form.getlist('add_task_ids')

        try:
            remove_ids = {int(task_id) for task_id in remove_ids}
            add_ids = [int(task_id) for task_id in add_ids]
        except ValueError:
            flash('Некорректные данные формы', 'error')
            return redirect(url_for('variants.variant_edit', variant_id=variant_id))

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
            return redirect(url_for('variants.variant_edit', variant_id=variant_id))

        Variant.update(variant_id, name=name)
        Variant.replace_tasks(variant_id, filtered_ids)

        if created_task_ids:
            flash(f'Вариант обновлён, добавлено новых задач: {len(created_task_ids)}', 'success')
        else:
            flash('Вариант успешно обновлён', 'success')
        return redirect(url_for('variants.variant_view', variant_id=variant_id))

    existing_task_ids = {task['id'] for task in current_tasks}
    all_tasks = Task.get_all()
    available_tasks = [task for task in all_tasks if task['id'] not in existing_task_ids]

    return render_template('teacher/variant_edit.html',
                          variant=variant,
                          current_tasks=current_tasks,
                          available_tasks=available_tasks)


@bp.route('/variants/<int:variant_id>/delete', methods=['POST'])
def variant_delete(variant_id):
    """Удаление варианта"""
    scope = request.form.get('scope', 'all')
    variant = Variant.get_by_id(variant_id)
    if variant:
        deleted = Variant.delete(variant_id, cascade=True)
        if deleted:
            flash(f'Вариант "{variant["name"]}" удалён вместе с результатами', 'success')
    return redirect(url_for('variants.variants_list', scope=scope))


@bp.route('/variants/delete-bulk', methods=['POST'])
def variant_delete_bulk():
    """Массовое удаление вариантов"""
    variant_ids = [int(vid) for vid in request.form.getlist('variant_ids') if str(vid).isdigit()]
    scope = request.form.get('scope', 'all')

    if not variant_ids:
        flash('Отметьте хотя бы один вариант для удаления', 'warning')
        return redirect(url_for('variants.variants_list', scope=scope))

    deleted_count = 0
    for variant_id in variant_ids:
        if Variant.delete(variant_id, cascade=True):
            deleted_count += 1

    flash(f'Удалено вариантов: {deleted_count}', 'success')
    return redirect(url_for('variants.variants_list', scope=scope))


@bp.route('/variants/upload', methods=['POST'])
def variant_upload():
    """Загрузка готового варианта с созданием задач в банке"""
    import uuid

    name = request.form.get('name')
    if not name:
        flash('Укажите название варианта', 'error')
        return redirect(url_for('variants.variant_create'))

    images = request.files.getlist('images')
    if not images or len(images) == 0:
        flash('Загрузите хотя бы одно изображение', 'error')
        return redirect(url_for('variants.variant_create'))

    # Фильтруем пустые файлы
    images = [img for img in images if img.filename]

    if len(images) == 0:
        flash('Загрузите хотя бы одно изображение', 'error')
        return redirect(url_for('variants.variant_create'))

    # Создаём вариант
    variant_id = Variant.create(name, 'uploaded')
    task_ids = []

    # Обрабатываем каждое изображение
    for i, image in enumerate(images):
        # Получаем данные формы для этой задачи
        ege_number_raw = request.form.get(f'ege_number_{i}')
        if ege_number_raw is None:
            continue
        ege_number = int(ege_number_raw)

        # Определяем тип задания
        answer_kind_val = request.form.get(f'answer_kind_{i}')
        is_file_upload = (answer_kind_val == 'file_upload')

        if is_file_upload:
            answer_count = 0
            answer1 = answer2 = answer_text = None
        else:
            answer_count = request.form.get(f'answer_count_{i}', type=int, default=1)
            if not answer_count or answer_count < 1:
                answer_count = 1
            if answer_count > 20:
                answer_count = 20
            answers = _read_answers_from_form(request.form, answer_count, suffix=f'_{i}')
            if any((v or '').strip() == '' for v in answers):
                continue
            answer1, answer2, answer_text = _pack_answers_for_task(answers)

        valid_image, image_error = _validate_upload(image, MAX_IMAGE_SIZE, ALLOWED_IMAGE_EXTENSIONS, 'png')
        if not valid_image:
            flash(f'Файл #{i + 1}: {image_error}', 'error')
            continue

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

        # ege_number=0 означает «Общий банк» (task_scope='class')
        task_scope = 'class' if ege_number == 0 else 'ege'
        stored_ege = ege_number if ege_number != 0 else 1

        # Создаём задачу в банке
        task_id = Task.create(
            ege_number=stored_ege,
            image_path=image_filename,
            answer_count=answer_count,
            answer_1=answer1,
            answer_2=answer2,
            answer_text=answer_text,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
            answer_kind='file_upload' if is_file_upload else 'classic',
            task_scope=task_scope,
        )
        task_ids.append(task_id)

    if not task_ids:
        flash('Не удалось создать ни одной задачи. Проверьте заполнение формы.', 'error')
        Variant.delete(variant_id)
        return redirect(url_for('variants.variant_create'))

    # Добавляем задачи в вариант (в порядке загрузки)
    Variant.add_tasks(variant_id, task_ids)

    flash(f'Вариант "{name}" создан! Добавлено задач: {len(task_ids)}', 'success')
    return redirect(url_for('variants.variant_view', variant_id=variant_id))


def _unique_variant_name(name):
    """«Урок 29.09» → «Урок 29.09 (2)», если такой вариант уже есть."""
    taken = {(v['name'] or '').casefold() for v in Variant.get_all()}
    if name.casefold() not in taken:
        return name
    number = 2
    while f'{name} ({number})'.casefold() in taken:
        number += 1
    return f'{name} ({number})'


def _image_extension(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpg'
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'webp'
    return None


def _store_variant_tasks(items, name=None, variant_id=None):
    """Записывает задания (ImportedTask) в «Общий банк» и в вариант: новый (name) или существующий (variant_id).
    Всё или ничего: при сбое удаляется всё созданное. Возвращает (variant_id, task_ids)."""
    class_id = ClassGroup.get_default_id()
    saved_files, task_ids, created_variant = [], [], None
    try:
        if variant_id is None:
            variant_id = created_variant = Variant.create(name, 'uploaded')
            place = catalog.guess(name)  # сразу в каталог по названию; учитель может поправить
            Variant.set_catalog(variant_id, place['grade'], place['topic'], place['kit'], place['minutes'] or '')
        for item in items:
            image_name = f'{uuid.uuid4().hex}.{_image_extension(item.image) or "png"}'
            with open(os.path.join(IMAGES_DIR, image_name), 'wb') as stream:
                saved_files.append(stream.name)
                stream.write(item.image)

            attachment_stored = None
            if item.attachment is not None:
                extension = item.attachment_name.rsplit('.', 1)[-1].lower()
                attachment_stored = f'{uuid.uuid4().hex}.{extension}'
                with open(os.path.join(ATTACHMENTS_DIR, attachment_stored), 'wb') as stream:
                    saved_files.append(stream.name)
                    stream.write(item.attachment)

            if item.mode == MODE_MULTILINE:
                # Ответ из нескольких строк: ученик видит большое поле, сравнение построчно
                answer_count, answer_1, answer_2, answer_text = 0, None, None, item.answers[0]
            else:
                answer_count = len(item.answers)
                answer_1, answer_2, answer_text = _pack_answers_for_task(item.answers)
            task_ids.append(Task.create(
                ege_number=1,  # номера ЕГЭ нет: задания живут в «Общем банке»
                image_path=image_name,
                answer_count=answer_count, answer_1=answer_1, answer_2=answer_2, answer_text=answer_text,
                attachment_path=attachment_stored, attachment_name=item.attachment_name,
                answer_kind='classic', task_scope='class', class_id=class_id))
        existing = [t['id'] for t in Variant.get_tasks(variant_id)] if created_variant is None else []
        Variant.replace_tasks(variant_id, existing + task_ids)
    except Exception:
        for task_id in task_ids:
            Task.delete(task_id)
        if created_variant is not None:
            Variant.delete(created_variant)
        for path in saved_files:
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    return variant_id, task_ids


def _read_builder_tasks(form, files):
    """Задания со страницы «Новый вариант». Сначала всё проверяется, потом что-то записывается."""
    try:
        count = int(form.get('task_count', '0'))
    except ValueError:
        count = 0
    if count < 1:
        raise VariantFileError('Добавьте хотя бы одно задание: вставьте скриншот (Ctrl+V) или выберите файл.')
    if count > MAX_LOCALSHOT_TASKS:
        raise VariantFileError(f'В варианте не может быть больше {MAX_LOCALSHOT_TASKS} заданий.')
    items, total = [], 0
    for index in range(count):
        number = index + 1
        image_file = files.get(f'image_{index}')
        if not image_file:
            raise VariantFileError(f'Задание {number}: нет картинки.')
        image = image_file.read(MAX_LOCALSHOT_IMAGE_SIZE + 1)
        if len(image) > MAX_LOCALSHOT_IMAGE_SIZE:
            raise VariantFileError(f'Задание {number}: картинка больше 30 МБ.')
        if not _image_extension(image):
            raise VariantFileError(f'Задание {number}: картинка должна быть PNG, JPG или WEBP.')
        mode = form.get(f'mode_{index}', MODE_FIELDS)
        if mode == MODE_MULTILINE:
            text = (form.get(f'text_{index}') or '').strip()
            if not text:
                raise VariantFileError(f'Задание {number}: введите ответ.')
            answers = [text]
        else:
            mode = MODE_FIELDS
            answers = [a.strip() for a in form.getlist(f'answer_{index}')]
            if not answers or any(not a for a in answers):
                raise VariantFileError(f'Задание {number}: заполните все поля ответа.')
            if len(answers) > MAX_TASK_ANSWERS:
                raise VariantFileError(f'Задание {number}: не больше {MAX_TASK_ANSWERS} полей ответа.')
        attachment = attachment_name = None
        attachment_file = files.get(f'attachment_{index}')
        if attachment_file and attachment_file.filename:
            attachment_name = clean_variant_name(attachment_file.filename.replace('\\', '/').rsplit('/', 1)[-1], 'файл')
            attachment = attachment_file.read(MAX_ATTACHMENT_SIZE + 1)
            if len(attachment) > MAX_ATTACHMENT_SIZE:
                raise VariantFileError(f'Задание {number}: файл к заданию больше 10 МБ.')
            check_attachment(number, attachment_name, attachment)
        total += len(image) + len(attachment or b'')
        if total > MAX_LOCALSHOT_FILE_SIZE:
            raise VariantFileError('Вариант слишком большой (больше 200 МБ).')
        items.append(ImportedTask(number, mode, answers, image, attachment_name, attachment))
    return items


@bp.route('/variants/builder', methods=['GET', 'POST'])
def variant_builder():
    """Новый вариант прямо здесь: скриншот (Ctrl+V, перетаскивание, файл) + ответ.
    С ?variant_id= — добавить задания в существующий вариант."""
    variant = None
    variant_id = request.values.get('variant_id', type=int)
    if variant_id:
        variant = Variant.get_by_id(variant_id)
        if not variant:
            flash('Вариант не найден', 'error')
            return redirect(url_for('variants.variants_list'))
    if request.method == 'GET':
        return render_template('teacher/variant_builder.html', variant=variant,
                               existing=len(Variant.get_tasks(variant_id)) if variant else 0,
                               default_name='Урок ' + datetime.now().strftime('%d.%m'),
                               allowed_files=', '.join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS)))
    try:
        items = _read_builder_tasks(request.form, request.files)
        if variant is None:
            name = clean_variant_name(request.form.get('name'), 'Урок ' + datetime.now().strftime('%d.%m'))
            name = _unique_variant_name(name)
        else:
            name = variant['name']
    except VariantFileError as error:
        return jsonify({'error': str(error)}), 400
    try:
        variant_id, task_ids = _store_variant_tasks(items, name=name, variant_id=variant['id'] if variant else None)
    except Exception:
        current_app.logger.exception('Не удалось сохранить вариант со страницы «Новый вариант»')
        return jsonify({'error': 'Не удалось сохранить: ошибка записи. Ничего не сохранено, попробуйте ещё раз.'}), 500
    if variant:
        flash(f'В вариант «{name}» добавлено: {tasks_word(len(task_ids))}.', 'success')
    else:
        flash(f'Вариант «{name}» создан: {tasks_word(len(task_ids))}.', 'success')
    return jsonify({'redirect': url_for('variants.variant_view', variant_id=variant_id)})


@bp.route('/variants/import-localshot', methods=['POST'])
def variant_import_localshot():
    """Загрузка файлов .variant из LocalShot — одного, нескольких или целой папки.
    Каждый файл — всё или ничего: испорченный файл пропускается, остальные загружаются.
    Вариант сразу встаёт в каталог: по «паспорту» из файла, а если его нет — по названию."""
    files = [f for f in request.files.getlist('file') if f and f.filename]
    files = [f for f in files if f.filename.lower().rsplit('.', 1)[-1] in ('variant', 'zip')] or files
    if not files:
        flash('Выберите файл варианта (.variant)', 'error')
        return redirect(url_for('variants.variants_list'))

    loaded, failed, last_id = [], [], None
    for file in files:
        title = os.path.basename(file.filename.replace('\\', '/'))
        valid, error = _validate_upload(file, MAX_LOCALSHOT_FILE_SIZE, {'variant', 'zip'}, None)
        if not valid:
            failed.append(f'«{title}»: {error}')
            continue
        data = file.stream.read()
        try:
            last_id, message = _import_variant_bytes(data, title)
        except ValueError as error:
            failed.append(f'«{title}»: {error}')
            continue
        remember_variant_file(hashlib.sha1(data).hexdigest(), title, last_id)
        loaded.append(message)
    return _finish_variant_import(loaded, failed, last_id)


def _import_variant_bytes(data, title):
    """Загружает один файл .variant: (id варианта, сообщение). ValueError с понятной причиной, если не вышло."""
    try:
        parsed = parse_variant_file(io.BytesIO(data), fallback_name=os.path.splitext(title)[0])
    except VariantFileError as error:
        raise ValueError(str(error)) from error
    except Exception as error:
        current_app.logger.exception('Не удалось прочитать файл варианта')
        raise ValueError('файл повреждён или это не вариант LocalShot') from error
    name = _unique_variant_name(parsed.name)
    try:
        variant_id, task_ids = _store_variant_tasks(parsed.tasks, name=name)
    except Exception as error:
        current_app.logger.exception('Не удалось загрузить вариант из LocalShot')
        raise ValueError('ошибка записи. Ничего не создано, попробуйте ещё раз') from error
    place = catalog.from_manifest(parsed.catalog, parsed.name)
    if not place['kit']:
        # «Тема» загружают после «Тема - вариант 2»: первый вариант тоже входит в комплект
        existing = {(v.get('kit') or '') for v in Variant.get_all()}
        if catalog.clean_field(parsed.name) in existing:
            place['kit'] = catalog.clean_field(parsed.name)
    Variant.set_catalog(variant_id, place['grade'], place['topic'], place['kit'], place['minutes'] or '')
    if place['kit']:
        _join_kit_base(place['kit'], place)
    message = f'Вариант «{name}» загружен из LocalShot. Заданий: {len(task_ids)}.'
    if name != parsed.name:
        message += f' Такой вариант уже был, поэтому этот назван «{name}».'
    where = ' → '.join(x for x in (place['grade'], place['topic'], place['kit']) if x)
    message += f' Место в каталоге: {where}.' if where else ' Класс и тему можно указать в карточке варианта.'
    return variant_id, message


@bp.route('/variants/import-folder', methods=['POST'])
def variant_import_folder():
    """Загружает из папки вариантов (Настройки) все файлы .variant, которых ещё нет в системе."""
    root = variant_folder.folder(get_setting)
    if not root or not os.path.isdir(root):
        flash('Папка с вариантами не найдена. Укажите её в «Настройках».', 'error')
        return redirect(url_for('variants.variants_list'))
    names = {v['name'] for v in Variant.get_all()}
    loaded, failed, last_id = [], [], None
    for path, digest, _name in variant_folder.new_files(root, known_variant_files(), names):
        title = os.path.relpath(path, root)
        try:
            with open(path, 'rb') as stream:
                data = stream.read(MAX_LOCALSHOT_FILE_SIZE + 1)
            if len(data) > MAX_LOCALSHOT_FILE_SIZE:
                raise ValueError('файл слишком большой')
            last_id, message = _import_variant_bytes(data, os.path.basename(path))
        except (OSError, ValueError) as error:
            failed.append(f'«{title}»: {error}')
            continue
        remember_variant_file(digest, path, last_id)
        loaded.append(message)
    if not loaded and not failed:
        flash('Новых вариантов в папке нет — всё уже загружено.', 'success')
        return redirect(url_for('variants.variants_list'))
    return _finish_variant_import(loaded, failed, last_id)


@bp.route('/variants/open-folder', methods=['POST'])
def variant_open_folder():
    if not variant_folder.open_in_explorer(variant_folder.folder(get_setting)):
        flash('Папку не удалось открыть. Проверьте путь в «Настройках».', 'error')
    return redirect(request.referrer or url_for('variants.variants_list'))


def _finish_variant_import(loaded, failed, last_id):
    if len(loaded) == 1:
        flash(loaded[0], 'success')
    elif loaded:
        flash(f'Загружено вариантов: {len(loaded)}. Они разложены по каталогу — проверьте классы и темы.', 'success')
    for problem in failed:
        flash(f'Не загружен {problem}', 'error')
    return redirect(url_for('variants.variants_list', variant_id=last_id) if loaded and len(loaded) == 1 else url_for('variants.variants_list'))


def _join_kit_base(kit, place):
    """Если первый вариант комплекта загружен раньше («Тема» без «вариант N»), добавить его в комплект."""
    for v in Variant.get_all():
        if not v.get('kit') and catalog.clean_field(v.get('name')) == kit:
            Variant.set_catalog(v['id'], v.get('grade') or place['grade'], v.get('topic') or place['topic'], kit,
                                v.get('minutes') or place['minutes'] or '')
