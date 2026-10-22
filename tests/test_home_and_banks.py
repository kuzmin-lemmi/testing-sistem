"""Главная страница без лишнего; банки заданий по номерам ЕГЭ скрыты, но включаются обратно.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import server  # noqa: E402
import blueprints.common  # noqa: E402
import student_shortcut  # noqa: E402


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_home_has_the_essentials_only(self):
        page = self.client.get('/').get_data(as_text=True)
        for text in ('Запустить тестирование', 'Загрузить из LocalShot', 'Скачать ярлык для учеников',
                     'Открыть результаты', '/test</code>'):
            self.assertIn(text, page)
        for text in ('Задачи по номерам ЕГЭ', 'Номеров ЕГЭ заполнено', 'Добавить задачу', 'Банк задач'):
            self.assertNotIn(text, page)

    def test_home_import_posts_straight_to_localshot_import(self):
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('action="/variants/import-localshot"', page)
        self.assertIn('id="homeImportFile"', page)

    def test_address_prefers_the_classroom_network(self):
        infos = [(2, 1, 6, '', (a, 0)) for a in ('10.8.1.2', '172.26.48.1', '192.168.0.77')]
        with patch('student_shortcut.socket.getaddrinfo', return_value=infos), \
                patch('student_shortcut.socket.socket', side_effect=OSError('нет')):
            self.assertEqual(student_shortcut.local_addresses()[0], '192.168.0.77')


class EgeBanksTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_hidden_by_default(self):
        self.assertFalse(blueprints.common.SHOW_EGE_BANKS)
        variants = self.client.get('/variants').get_data(as_text=True)
        self.assertNotIn('Банк задач', variants)
        self.assertNotIn('Создать вариант', variants)
        self.assertNotIn('Обычные классы', variants)
        self.assertNotIn('value="individual"', self.client.get('/sessions/new').get_data(as_text=True))

    def test_pages_still_exist(self):
        self.assertEqual(self.client.get('/tasks').status_code, 200)
        self.assertEqual(self.client.get('/variants/create').status_code, 200)

    def test_can_be_switched_back_on(self):
        with patch('blueprints.common.SHOW_EGE_BANKS', True):
            self.assertIn('Банк задач', self.client.get('/variants').get_data(as_text=True))
            self.assertIn('Создать вариант', self.client.get('/variants').get_data(as_text=True))
            self.assertIn('value="individual"', self.client.get('/sessions/new').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
