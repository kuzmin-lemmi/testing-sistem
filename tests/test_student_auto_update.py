"""Страницы ученика обновляются сами; вход по коду скрыт из интерфейса, но не удалён.

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
from models import Task, TestSession, Variant  # noqa: E402

STUDENT_PC = {'REMOTE_ADDR': '192.168.1.77'}


def close_all_sessions():
    for item in TestSession.get_all():
        if item['status'] == 'active':
            TestSession.close(item['id'])


def make_variant(name='Автообновление'):
    task_id = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='1')
    variant_id = Variant.create(name, 'uploaded')
    Variant.add_tasks(variant_id, [task_id])
    return variant_id


class StateTests(unittest.TestCase):
    def setUp(self):
        close_all_sessions()
        self.client = server.app.test_client()

    def test_state_without_a_running_test(self):
        response = self.client.get('/test/state', environ_base=STUDENT_PC)
        self.assertEqual(response.get_json(), {'active': False, 'session_id': None})
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_state_follows_the_teacher(self):
        session_id = TestSession.create(make_variant(), 0, 45, None, 1, 0, 0, 0)
        self.assertEqual(self.client.get('/test/state', environ_base=STUDENT_PC).get_json(),
                         {'active': True, 'session_id': session_id})
        TestSession.close(session_id)
        self.assertEqual(self.client.get('/test/state', environ_base=STUDENT_PC).get_json()['active'], False)


class PagesWatchTheServerTests(unittest.TestCase):
    def setUp(self):
        close_all_sessions()
        self.client = server.app.test_client()

    def test_waiting_page_no_longer_reloads_by_timer_it_watches(self):
        page = self.client.get('/test', environ_base=STUDENT_PC).get_data(as_text=True)
        self.assertIn('data-watch="none"', page)
        self.assertIn('js/student_watch.js', page)
        self.assertNotIn('location.reload', page)

    def test_login_page_knows_which_test_it_shows(self):
        session_id = TestSession.create(make_variant(), 0, 45, None, 1, 0, 0, 0)
        page = self.client.get('/test', environ_base=STUDENT_PC).get_data(as_text=True)
        self.assertIn(f'data-watch="login" data-watch-session="{session_id}"', page)
        self.assertNotIn('name="code"', page)  # без кода поля нет

    def test_result_page_leaves_when_a_new_test_starts(self):
        variant_id = make_variant()
        session_id = TestSession.create(variant_id, 0, 45, None, 1, 0, 0, 0)
        self.assertEqual(self.client.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'},
                                          environ_base=STUDENT_PC).status_code, 302)
        self.client.post('/test/finish', json={}, environ_base=STUDENT_PC)
        page = self.client.get('/test/result', environ_base=STUDENT_PC).get_data(as_text=True)
        self.assertIn(f'data-watch="result" data-watch-session="{session_id}"', page)

    def test_watcher_script_rules(self):
        with open(os.path.join(os.path.dirname(server.__file__), 'static', 'js', 'student_watch.js'),
                  encoding='utf-8') as stream:
            script = stream.read()
        self.assertIn("/test/state", script)
        self.assertIn("mode === 'none') change = current !== 0", script)
        self.assertIn("mode === 'login') change = current !== shown", script)
        self.assertIn("mode === 'result') change = current !== 0 && current !== shown", script)


class AccessCodeTests(unittest.TestCase):
    """Вход по коду скрыт из интерфейса, но возможность осталась в системе."""

    def setUp(self):
        close_all_sessions()
        self.client = server.app.test_client()
        self.client.get('/sessions/new')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']

    def start_test(self, **extra):
        variant_id = make_variant()
        data = {'csrf_token': self.token, 'variant_mode': 'single', 'variant_id': str(variant_id),
                'time_limit': '45', 'total_tasks': '1', 'grade_5_min': '1', 'grade_4_min': '1', 'grade_3_min': '1'}
        data.update(extra)
        self.assertEqual(self.client.post('/sessions/new', data=data).status_code, 302)
        return TestSession.get_active()

    def test_form_has_no_code_option_by_default(self):
        page = self.client.get('/sessions/new').get_data(as_text=True)
        self.assertNotIn('use_code', page)
        self.assertNotIn('Требовать код', page)

    def test_new_test_has_no_code_and_students_enter_by_name_only(self):
        active = self.start_test()
        self.assertIsNone(active['access_code'])
        response = self.client.post('/test/start', data={'first_name': 'Борис', 'last_name': 'Яковлев'},
                                    environ_base=STUDENT_PC)
        self.assertEqual(response.status_code, 302)

    def test_option_can_be_switched_back_on(self):
        with patch('blueprints.common.SHOW_ACCESS_CODE_OPTION', True):
            page = self.client.get('/sessions/new').get_data(as_text=True)
        self.assertIn('name="use_code"', page)

    def test_code_still_works_in_the_system(self):
        active = self.start_test(use_code='1')
        self.assertRegex(active['access_code'], r'^[A-Z0-9]{4}$')
        wrong = self.client.post('/test/start', data={'first_name': 'Вера', 'last_name': 'Ким', 'code': 'нет'},
                                 environ_base=STUDENT_PC, follow_redirects=True)
        self.assertIn('Неверный код доступа', wrong.get_data(as_text=True))
        login = self.client.get('/test', environ_base=STUDENT_PC).get_data(as_text=True)
        self.assertIn('name="code"', login)  # у тестирования с кодом поле по-прежнему есть


if __name__ == '__main__':
    unittest.main()
