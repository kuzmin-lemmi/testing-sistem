"""Главная страница: понятные события вместо логов, один адрес для учеников, остановка с подтверждением.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import activity  # noqa: E402
import server  # noqa: E402
from models import Task, TestSession, Variant  # noqa: E402

PUPIL_PC = {'REMOTE_ADDR': '192.168.0.45'}


def texts():
    return [e['text'] for e in activity.recent_events(100)]


class SystemStatusTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        variant = Variant.create('События', 'uploaded')
        Variant.add_tasks(variant, [task])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 0, 0)
        activity._seen_computers.discard('192.168.0.45')
        activity._events.clear()

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def test_new_computer_and_student_are_described_in_plain_words(self):
        pupil = server.app.test_client()
        pupil.get('/test', environ_base=PUPIL_PC)
        pupil.post('/test/start', data={'first_name': 'Пётр', 'last_name': 'Иванов'}, environ_base=PUPIL_PC)
        events = ' | '.join(texts())
        self.assertIn('подключился', events)
        self.assertIn('192.168.0.45', events)
        self.assertIn('Иванов Пётр вошёл в тест', events)

    def test_same_computer_is_announced_once(self):
        pupil = server.app.test_client()
        for _ in range(5):
            pupil.get('/test/alive', environ_base=PUPIL_PC)
        self.assertEqual(sum('192.168.0.45' in t and 'подключился' in t for t in texts()), 1)

    def test_status_json_and_home_page(self):
        status = server.app.test_client().get('/system/status').get_json()
        for key in ('address', 'firewall', 'computers', 'events', 'uptime'):
            self.assertIn(key, status)
        self.assertRegex(status['address'], r'^http://[\d.]+:\d+/test$')
        page = server.app.test_client().get('/').get_data(as_text=True)
        self.assertIn('Что происходит в системе', page)

    def test_students_cannot_open_system_pages(self):
        pupil = server.app.test_client()
        for path in ('/system/status', '/system/log'):
            self.assertEqual(pupil.get(path, environ_base=PUPIL_PC).status_code, 403)
        self.assertEqual(pupil.post('/system/stop', environ_base=PUPIL_PC).status_code, 403)

    def test_technical_log_does_not_need_a_console(self):
        activity.setup_logging()
        activity.log_event('проверка журнала', 'info')
        with open(activity.LOG_FILE, encoding='utf-8') as stream:
            self.assertIn('проверка журнала', stream.read())


if __name__ == '__main__':
    unittest.main()
