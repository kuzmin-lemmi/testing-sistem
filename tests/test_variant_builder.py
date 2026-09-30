"""«Новый вариант» прямо в системе: скриншоты + ответы, добавление заданий в готовый вариант.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import unittest

if 'EGE_DATA_DIR' not in os.environ:
    os.environ['EGE_DATA_DIR'] = tempfile.mkdtemp(prefix='ege-test-')
    atexit.register(shutil.rmtree, os.environ['EGE_DATA_DIR'], True)
_DATA = os.environ['EGE_DATA_DIR']
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import results_logic  # noqa: E402
import server  # noqa: E402
from models import Task, Variant  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 50
JPG = b'\xff\xd8\xff\xe0' + b'0' * 50


def files_in(folder):
    return sorted(os.listdir(os.path.join(_DATA, folder)))


class BuilderTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.client.get('/variants/builder')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        self.before = {v['id'] for v in Variant.get_all()}
        self.before_tasks = len(Task.get_all())
        self.before_images = files_in('images')

    def send(self, tasks, name='Урок из скриншотов', variant_id=None, token=True):
        data = {'task_count': str(len(tasks))}
        if name is not None:
            data['name'] = name
        for i, task in enumerate(tasks):
            if task.get('image', PNG) is not None:
                data[f'image_{i}'] = (io.BytesIO(task.get('image', PNG)), f'task{i}.png')
            data[f'mode_{i}'] = task.get('mode', 'fields')
            if 'text' in task:
                data[f'text_{i}'] = task['text']
            if 'answers' in task:
                data[f'answer_{i}'] = task['answers']
            if 'attachment' in task:
                data[f'attachment_{i}'] = (io.BytesIO(task['attachment'][1]), task['attachment'][0])
        headers = {'X-CSRFToken': self.token} if token else {}
        url = '/variants/builder' + (f'?variant_id={variant_id}' if variant_id else '')
        return self.client.post(url, data=data, headers=headers, content_type='multipart/form-data')

    def new_variants(self):
        return [v for v in Variant.get_all() if v['id'] not in self.before]

    def assert_nothing_created(self):
        self.assertEqual(self.new_variants(), [])
        self.assertEqual(len(Task.get_all()), self.before_tasks)
        self.assertEqual(files_in('images'), self.before_images)

    def test_page(self):
        page = self.client.get('/variants/builder').get_data(as_text=True)
        self.assertIn('Ctrl</kbd>+<kbd>V', page)
        self.assertIn('Сохранить вариант', page)
        self.assertIn('/variants/builder', self.client.get('/variants').get_data(as_text=True))
        self.assertIn('/variants/builder', self.client.get('/').get_data(as_text=True))

    def test_create_variant(self):
        response = self.send([
            {'answers': ['42']},
            {'answers': ['1', '2', '3'], 'image': JPG},
            {'mode': 'multiline', 'text': '5 10\n7 3'},
            {'answers': ['да'], 'attachment': ('Данные 17.txt', b'1\n2')},
        ])
        self.assertEqual(response.status_code, 200)
        (variant,) = self.new_variants()
        self.assertTrue(response.get_json()['redirect'].endswith(f"/variants/{variant['id']}"))
        self.assertEqual(variant['name'], 'Урок из скриншотов')
        tasks = Variant.get_tasks(variant['id'])
        self.assertEqual([results_logic.format_expected(t) for t in tasks], ['42', '1, 2, 3', '5 10 / 7 3', 'да'])
        self.assertTrue(tasks[1]['image_path'].endswith('.jpg'))
        self.assertEqual(tasks[3]['attachment_name'], 'Данные 17.txt')
        self.assertEqual({t['task_scope'] for t in tasks}, {'class'})

    def test_add_tasks_to_existing_variant(self):
        self.send([{'answers': ['1']}], name='Дополняемый')
        (variant,) = self.new_variants()
        response = self.send([{'answers': ['2']}, {'answers': ['3']}], name=None, variant_id=variant['id'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual([results_logic.format_expected(t) for t in Variant.get_tasks(variant['id'])], ['1', '2', '3'])
        self.assertEqual(len(self.new_variants()), 1)
        self.assertIn('Добавить задания', self.client.get(f"/variants/builder?variant_id={variant['id']}").get_data(as_text=True))

    def test_same_name_gets_number(self):
        self.send([{'answers': ['1']}], name='Повтор из скриншотов')
        self.send([{'answers': ['1']}], name='Повтор из скриншотов')
        self.assertEqual(sorted(v['name'] for v in self.new_variants()), ['Повтор из скриншотов', 'Повтор из скриншотов (2)'])

    def test_errors_create_nothing(self):
        cases = [
            ([], 'хотя бы одно задание'),
            ([{'answers': ['1']}, {'answers': ['']}], 'Задание 2: заполните все поля ответа'),
            ([{'mode': 'multiline', 'text': '  '}], 'Задание 1: введите ответ'),
            ([{'answers': ['1'], 'image': b'GIF89a...'}], 'Задание 1: картинка должна быть PNG, JPG или WEBP'),
            ([{'answers': ['1'], 'image': None}], 'Задание 1: нет картинки'),
            ([{'answers': ['1'], 'attachment': ('run.exe', b'MZ')}], 'такого типа не принимается'),
            ([{'answers': [str(i) for i in range(21)]}], 'не больше 20 полей'),
        ]
        for tasks, text in cases:
            with self.subTest(text=text):
                response = self.send(tasks)
                self.assertEqual(response.status_code, 400)
                self.assertIn(text, response.get_json()['error'])
                self.assert_nothing_created()

    def test_csrf_required(self):
        self.assertEqual(self.send([{'answers': ['1']}], token=False).status_code, 400)
        self.assert_nothing_created()

    def test_student_sees_jpg_task(self):
        self.send([{'answers': ['1'], 'image': JPG}])
        (variant,) = self.new_variants()
        (task,) = Variant.get_tasks(variant['id'])
        response = self.client.get('/images/' + task['image_path'], environ_base={'REMOTE_ADDR': '192.168.1.9'})
        self.assertEqual(response.get_data(), JPG)
        response.close()


if __name__ == '__main__':
    unittest.main()
