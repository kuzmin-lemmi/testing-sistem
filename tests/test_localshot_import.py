"""Загрузка варианта из LocalShot (.variant).

Запуск из корня проекта:  python -m unittest discover -s tests -v
База и файлы создаются во временной папке, рабочие данные не затрагиваются.
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

if 'EGE_DATA_DIR' not in os.environ:  # одна временная папка на все тесты
    os.environ['EGE_DATA_DIR'] = tempfile.mkdtemp(prefix='ege-test-')
    atexit.register(shutil.rmtree, os.environ['EGE_DATA_DIR'], True)
_DATA = os.environ['EGE_DATA_DIR']
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import server  # noqa: E402
from models import Answer, ClassGroup, Student, Task, TestSession, Variant  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


def make_variant_file(tasks, name='Урок 29.09', version=1, manifest_extra=None):
    """Собирает .variant так же, как LocalShot. tasks: список словарей с ключами
    id, mode, answers, image, attachment_name, attachment (последние три необязательны)."""
    items = []
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for task in tasks:
            item = {'id': task['id'], 'mode': task.get('mode', 'fields'), 'answers': task['answers']}
            if task.get('attachment_name'):
                item['attachment_name'] = task['attachment_name']
                archive.writestr(f"files/{task['id']}.bin", task.get('attachment', b''))
            if task.get('image', PNG) is not None:
                archive.writestr(f"images/{task['id']}.png", task.get('image', PNG))
            items.append(item)
        manifest = {'version': version, 'name': name, 'tasks': items}
        manifest.update(manifest_extra or {})
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
    return buffer.getvalue()


def files_in(folder):
    return sorted(os.listdir(os.path.join(_DATA, folder)))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.client.get('/variants')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        self.before_variants = {v['id'] for v in Variant.get_all()}
        self.before_tasks = len(Task.get_all())
        self.before_images = files_in('images')
        self.before_attachments = files_in('attachments')

    def upload(self, data, filename='Урок.variant'):
        return self.client.post('/variants/import-localshot', follow_redirects=True, content_type='multipart/form-data',
                                data={'csrf_token': self.token, 'file': (io.BytesIO(data), filename)})

    def new_variants(self):
        return [v for v in Variant.get_all() if v['id'] not in self.before_variants]

    def assert_nothing_created(self):
        self.assertEqual(self.new_variants(), [])
        self.assertEqual(len(Task.get_all()), self.before_tasks)
        self.assertEqual(files_in('images'), self.before_images)
        self.assertEqual(files_in('attachments'), self.before_attachments)

    # ---- успешная загрузка --------------------------------------------------
    def test_full_variant_is_imported_in_order(self):
        data = make_variant_file([
            {'id': 'aaaaaa000001', 'answers': ['42']},
            {'id': 'aaaaaa000002', 'answers': ['5', 'да']},
            {'id': 'aaaaaa000003', 'answers': ['1', '2', '3', '4']},
            {'id': 'aaaaaa000004', 'mode': 'multiline', 'answers': ['5 10\n7 3']},
            {'id': 'aaaaaa000005', 'answers': ['7'], 'attachment_name': 'Данные 17.txt', 'attachment': b'1\n2\n3'},
        ], name='Контрольная 11Б')
        response = self.upload(data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Вариант «Контрольная 11Б» загружен из LocalShot. Заданий: 5.', response.get_data(as_text=True))

        (variant,) = self.new_variants()
        self.assertEqual((variant['name'], variant['variant_type']), ('Контрольная 11Б', 'uploaded'))
        tasks = Variant.get_tasks(variant['id'])
        self.assertEqual([t['position'] for t in tasks], [1, 2, 3, 4, 5])

        one, two, four, text, with_file = tasks
        self.assertEqual((one['answer_count'], str(one['answer_1'])), (1, '42'))  # колонка целочисленная: 42 хранится числом
        self.assertEqual((two['answer_count'], str(two['answer_1']), two['answer_2']), (2, '5', 'да'))
        self.assertEqual((four['answer_count'], four['answer_text']), (4, '3\n4'))
        self.assertEqual((text['answer_count'], text['answer_1'], text['answer_text']), (0, None, '5 10\n7 3'))
        self.assertEqual(text['answer_kind'], 'classic')

        default_class = ClassGroup.get_default_id()
        for task in tasks:
            self.assertEqual((task['task_scope'], task['class_id']), ('class', default_class))
            with open(os.path.join(_DATA, 'images', task['image_path']), 'rb') as stream:
                self.assertEqual(stream.read(), PNG)
        self.assertIsNone(one['attachment_path'])
        self.assertEqual(with_file['attachment_name'], 'Данные 17.txt')
        with open(os.path.join(_DATA, 'attachments', with_file['attachment_path']), 'rb') as stream:
            self.assertEqual(stream.read(), b'1\n2\n3')

    def test_imported_tasks_are_checked_like_normal_ones(self):
        data = make_variant_file([
            {'id': 'bbbbbb000001', 'answers': ['012']},
            {'id': 'bbbbbb000002', 'answers': ['1', '2', 'Да']},
            {'id': 'bbbbbb000003', 'mode': 'multiline', 'answers': ['5 10\n7 3']},
        ])
        self.upload(data)
        (variant,) = self.new_variants()
        first, second, third = Variant.get_tasks(variant['id'])
        session_id = TestSession.create(variant['id'], 0, 45, None, 1, 0, 0, 0)
        student_id = Student.create(session_id, 'Анна', 'Петрова', variant['id'])
        self.assertTrue(Answer.save(student_id, first['id'], '12'))
        self.assertFalse(Answer.save(student_id, first['id'], '13'))
        self.assertTrue(Answer.save(student_id, second['id'], '1', '2', answer_text='да'))
        self.assertFalse(Answer.save(student_id, second['id'], '1', '2', answer_text='нет'))
        self.assertTrue(Answer.save(student_id, third['id'], None, None, answer_text='5  10\n7 3\n'))
        self.assertFalse(Answer.save(student_id, third['id'], None, None, answer_text='5 10'))

    def test_same_name_gets_number_and_old_variant_is_untouched(self):
        data = make_variant_file([{'id': 'cccccc000001', 'answers': ['1']}], name='Повтор')
        self.upload(data)
        self.upload(data)
        response = self.upload(data)
        self.assertIn('назван «Повтор (3)»', response.get_data(as_text=True))
        names = sorted(v['name'] for v in self.new_variants())
        self.assertEqual(names, ['Повтор', 'Повтор (2)', 'Повтор (3)'])
        for variant in self.new_variants():
            self.assertEqual(len(Variant.get_tasks(variant['id'])), 1)

    def test_name_falls_back_to_file_name(self):
        data = make_variant_file([{'id': 'dddddd000001', 'answers': ['1']}], manifest_extra={'name': '  '})
        self.upload(data, filename='Зачёт по циклам.variant')
        self.assertEqual([v['name'] for v in self.new_variants()], ['Зачёт по циклам'])

    def test_new_attachment_types_are_accepted(self):
        pdf = b'%PDF-1.4 ...'
        zipped = make_variant_file([{'id': 'eeeeee000009', 'answers': ['1']}])  # любой настоящий ZIP
        data = make_variant_file([
            {'id': 'eeeeee000001', 'answers': ['1'], 'attachment_name': 'условие.pdf', 'attachment': pdf},
            {'id': 'eeeeee000002', 'answers': ['1'], 'attachment_name': 'таблица.docx', 'attachment': zipped},
            {'id': 'eeeeee000003', 'answers': ['1'], 'attachment_name': 'заготовка.py', 'attachment': b'print(1)'},
            {'id': 'eeeeee000004', 'answers': ['1'], 'attachment_name': 'файлы.zip', 'attachment': zipped},
            {'id': 'eeeeee000005', 'answers': ['1'], 'attachment_name': 'текст.odt', 'attachment': zipped},
        ])
        self.upload(data)
        (variant,) = self.new_variants()
        names = [t['attachment_name'] for t in Variant.get_tasks(variant['id'])]
        self.assertEqual(names, ['условие.pdf', 'таблица.docx', 'заготовка.py', 'файлы.zip', 'текст.odt'])

    def test_student_downloads_file_under_its_real_name(self):
        self.upload(make_variant_file([{'id': 'f1f1f1000001', 'answers': ['1'],
                                        'attachment_name': 'Данные 17.txt', 'attachment': b'1\n2'}]))
        (variant,) = self.new_variants()
        (task,) = Variant.get_tasks(variant['id'])
        response = self.client.get('/attachments/' + task['attachment_path'])
        self.assertEqual(response.get_data(), b'1\n2')
        self.assertIn("filename*=UTF-8''%D0%94%D0%B0%D0%BD%D0%BD%D1%8B%D0%B5%2017.txt", response.headers['Content-Disposition'])
        response.close()

    def test_attachment_name_cannot_contain_path(self):
        data = make_variant_file([{'id': 'ffffff000001', 'answers': ['1'],
                                   'attachment_name': '..\\..\\evil\\17.txt', 'attachment': b'1'}])
        self.upload(data)
        (variant,) = self.new_variants()
        (task,) = Variant.get_tasks(variant['id'])
        self.assertEqual(task['attachment_name'], '17.txt')
        self.assertRegex(task['attachment_path'], r'^[0-9a-f]{32}\.txt$')

    def test_big_screenshot_over_5_mb_is_accepted(self):
        big = PNG + b'0' * (6 * 1024 * 1024)
        self.upload(make_variant_file([{'id': 'abcabc000001', 'answers': ['1'], 'image': big}]))
        self.assertEqual(len(self.new_variants()), 1)

    # ---- плохие файлы: ничего не создаётся ---------------------------------------
    def check_refused(self, data, text, filename='Урок.variant'):
        response = self.upload(data, filename)
        self.assertIn(text, response.get_data(as_text=True))
        self.assert_nothing_created()

    def test_not_a_zip(self):
        self.check_refused('просто текст'.encode('utf-8'), 'Это не файл варианта LocalShot')

    def test_wrong_extension(self):
        self.check_refused(make_variant_file([{'id': 'a1a1a1000001', 'answers': ['1']}]), 'Недопустимое расширение',
                           filename='Урок.txt')

    def test_damaged_zip_gives_clear_message_not_a_crash(self):
        good = make_variant_file([{'id': 'a1b2c3000001', 'answers': ['1']}, {'id': 'a1b2c3000002', 'answers': ['2']}])
        for name, data in [('без начала', good[99:]), ('обрезан', good[:len(good) // 2]),
                           ('испорчена середина', good[:200] + bytes([255]) * 60 + good[260:])]:
            with self.subTest(name):
                response = self.upload(data)
                self.assertEqual(response.status_code, 200)
                self.assertRegex(response.get_data(as_text=True), 'повреждён|Это не файл варианта|слишком')
                self.assert_nothing_created()

    def test_zip_without_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('hello.txt', 'hi')
        self.check_refused(buffer.getvalue(), 'нет части «manifest.json»')

    def test_broken_manifest(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr('manifest.json', '{not json')
        self.check_refused(buffer.getvalue(), 'не читается описание заданий')

    def test_empty_variant(self):
        self.check_refused(make_variant_file([]), 'нет ни одного задания')

    def test_newer_format(self):
        self.check_refused(make_variant_file([{'id': 'a2a2a2000001', 'answers': ['1']}], version=99),
                           'более новой версией LocalShot')

    def test_missing_image_after_valid_first_task_creates_nothing(self):
        data = make_variant_file([{'id': 'a3a3a3000001', 'answers': ['1']},
                                  {'id': 'a3a3a3000002', 'answers': ['2'], 'image': None}])
        self.check_refused(data, 'картинка задания 2')

    def test_image_must_be_png(self):
        self.check_refused(make_variant_file([{'id': 'a4a4a4000001', 'answers': ['1'], 'image': b'GIF89a....'}]),
                           'картинка не является PNG')

    def test_image_over_limit(self):
        with patch('localshot_import.MAX_LOCALSHOT_IMAGE_SIZE', 100):
            self.check_refused(make_variant_file([{'id': 'a5a5a5000001', 'answers': ['1'], 'image': PNG + b'0' * 200}]),
                               'слишком большой')

    def test_forbidden_attachment_type_names_the_task(self):
        data = make_variant_file([{'id': 'a6a6a6000001', 'answers': ['1']},
                                  {'id': 'a6a6a6000002', 'answers': ['1'], 'attachment_name': 'run.exe', 'attachment': b'MZ'}])
        self.check_refused(data, 'Задание 2: файл «run.exe» такого типа не принимается')

    def test_attachment_content_must_match_type(self):
        self.check_refused(make_variant_file([{'id': 'a7a7a7000001', 'answers': ['1'],
                                               'attachment_name': 'a.pdf', 'attachment': b'not a pdf'}]),
                           'повреждён или не соответствует своему типу')

    def test_bad_task_data(self):
        cases = [
            ({'id': '../etc', 'answers': ['1']}, 'неверный номер задания'),
            ({'id': 'a8a8a8000001', 'answers': []}, 'неверное число ответов'),
            ({'id': 'a8a8a8000001', 'answers': ['1', '  ']}, 'есть пустой ответ'),
            ({'id': 'a8a8a8000001', 'answers': [str(i) for i in range(1, 22)]}, 'неверное число ответов'),
            ({'id': 'a8a8a8000001', 'mode': 'multiline', 'answers': ['1', '2']}, 'должен быть одним текстом'),
            ({'id': 'a8a8a8000001', 'mode': 'weird', 'answers': ['1']}, 'неизвестный тип ответа'),
        ]
        for task, text in cases:
            with self.subTest(text=text):
                self.check_refused(make_variant_file([task]), text)

    def test_duplicate_task_ids(self):
        data = make_variant_file([{'id': 'a9a9a9000001', 'answers': ['1']}, {'id': 'a9a9a9000001', 'answers': ['2']}])
        self.check_refused(data, 'неверный номер задания')

    def test_no_file_chosen(self):
        response = self.client.post('/variants/import-localshot', follow_redirects=True,
                                    data={'csrf_token': self.token})
        self.assertIn('Выберите файл варианта', response.get_data(as_text=True))
        self.assert_nothing_created()

    def test_csrf_is_required(self):
        response = self.client.post('/variants/import-localshot', content_type='multipart/form-data',
                                    data={'file': (io.BytesIO(b'x'), 'a.variant')})
        self.assertEqual(response.status_code, 400)

    # ---- сбой посреди записи ----------------------------------------------------
    def test_failure_in_the_middle_rolls_everything_back(self):
        data = make_variant_file([
            {'id': 'b1b1b1000001', 'answers': ['1'], 'attachment_name': 'a.txt', 'attachment': b'1'},
            {'id': 'b1b1b1000002', 'answers': ['2'], 'attachment_name': 'b.txt', 'attachment': b'2'},
            {'id': 'b1b1b1000003', 'answers': ['3']},
        ])
        real_create = Task.create
        calls = []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:
                raise OSError('диск полон')
            return real_create(*args, **kwargs)

        with patch.object(server.Task, 'create', side_effect=flaky), self.assertLogs(server.app.logger, 'ERROR'):
            response = self.upload(data)
        self.assertIn('Ничего не создано', response.get_data(as_text=True))
        self.assertEqual(len(calls), 3)
        self.assert_nothing_created()

    # ---- страница ------------------------------------------------------------
    def test_variants_page_has_import_button_and_shows_imported_variant(self):
        self.upload(make_variant_file([{'id': 'c1c1c1000001', 'answers': ['42']},
                                       {'id': 'c1c1c1000002', 'mode': 'multiline', 'answers': ['5 10\n7 3']}],
                                      name='Страница'))
        (variant,) = self.new_variants()
        html = self.client.get(f"/variants?variant_id={variant['id']}").get_data(as_text=True)
        self.assertIn('Загрузить из LocalShot', html)
        self.assertIn('importLocalshotForm', html)
        self.assertIn('title="Правильный ответ">42</span>', html)
        self.assertIn('5 10', html)
        self.assertNotIn('№1', html)  # у заданий из общего банка нет номера ЕГЭ


class SharedBrowserTests(unittest.TestCase):
    """Учитель и ученик в одном браузере (так проверяют дома): вход ученика не должен ломать кнопки учителя."""

    def test_student_pages_do_not_reset_teacher_csrf_token(self):
        client = server.app.test_client()
        client.get('/variants')
        with client.session_transaction() as session:
            token = session['_csrf_token']
        client.post('/variants/import-localshot', content_type='multipart/form-data',
                    data={'csrf_token': token, 'file': (io.BytesIO(make_variant_file(
                        [{'id': 'd1d1d1000001', 'answers': ['1']}], name='Один браузер')), 'v.variant')})
        variant = next(v for v in Variant.get_all() if v['name'] == 'Один браузер')
        session_id = TestSession.create(variant['id'], 0, 45, None, 1, 0, 0, 0)

        self.assertEqual(client.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}).status_code, 302)
        client.post('/test/finish', json={})
        client.get('/test/result')      # результат ученика раньше сбрасывал весь сеанс
        client.get('/test')             # и страница входа тоже
        with client.session_transaction() as session:
            self.assertEqual(session.get('_csrf_token'), token)
            self.assertNotIn('student_id', session)

        response = client.post(f'/sessions/{session_id}/close', data={'csrf_token': token})
        self.assertEqual(response.status_code, 302)  # 400 «Недействительный CSRF-токен» было раньше


if __name__ == '__main__':
    unittest.main()
