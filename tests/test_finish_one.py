"""Учитель завершает тест одному ученику: у него сразу оценка (без правильных ответов, пока другие решают),
ответы больше не принимаются, войти снова нельзя; остальные продолжают.

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

import server  # noqa: E402
from models import Student, Task, TestSession, Variant  # noqa: E402

ANNA = {'REMOTE_ADDR': '192.168.0.41'}
OLEG = {'REMOTE_ADDR': '192.168.0.42'}


class FinishOneStudentTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.t1 = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        self.t2 = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='8')
        variant = Variant.create('Досрочно', 'uploaded')
        Variant.add_tasks(variant, [self.t1, self.t2])
        # показывать ответы: да; завершает только учитель: да
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 0, 0)
        self.anna = self.join('Анна', 'Петрова', ANNA)
        self.oleg = self.join('Олег', 'Сидоров', OLEG)
        self.anna_id = Student.get_by_session_and_name(self.session_id, 'Анна', 'Петрова')['id']
        self.oleg_id = Student.get_by_session_and_name(self.session_id, 'Олег', 'Сидоров')['id']
        self.teacher = server.app.test_client()
        self.teacher.get('/')
        with self.teacher.session_transaction() as s:
            self.csrf = s['_csrf_token']

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def join(self, first, last, pc):
        client = server.app.test_client()
        client.post('/test/start', data={'first_name': first, 'last_name': last}, environ_base=pc)
        return client

    def finish_anna(self):
        return self.teacher.post(f'/sessions/{self.session_id}/students/{self.anna_id}/finish',
                                 headers={'X-CSRFToken': self.csrf})

    def test_student_gets_grade_and_others_continue(self):
        self.anna.post('/test/save', json={'task_id': self.t1, 'answer_1': '7'}, environ_base=ANNA)
        self.assertTrue(self.finish_anna().get_json()['ok'])
        self.assertEqual(Student.get_by_id(self.anna_id)['status'], 'finished')

        # её страница при следующем опросе уходит на оценку
        ping = self.anna.post('/test/ping', environ_base=ANNA).get_json()
        self.assertEqual(ping['redirect'], '/test/result')
        page = self.anna.get('/test/exam', follow_redirects=True, environ_base=ANNA).get_data(as_text=True)
        self.assertIn('score-number', page)  # страница с оценкой
        self.assertNotIn('data-task-id', page)
        self.assertNotIn('Правильный', page)  # остальные ещё решают — правильные ответы не показываем

        # Олег решает дальше
        self.assertEqual(self.oleg.post('/test/save', json={'task_id': self.t2, 'answer_1': '8'},
                                        environ_base=OLEG).status_code, 200)
        self.assertTrue(self.oleg.post('/test/ping', environ_base=OLEG).get_json()['active'])

    def test_finished_student_cannot_answer_or_come_back(self):
        self.finish_anna()
        self.assertEqual(self.anna.post('/test/save', json={'task_id': self.t2, 'answer_1': '8'},
                                        environ_base=ANNA).status_code, 403)
        again = self.join('Анна', 'Петрова', ANNA)
        self.assertNotIn('data-task-id', again.get('/test/exam', follow_redirects=True,
                                                   environ_base=ANNA).get_data(as_text=True))

    def test_after_everyone_finishes_answers_are_shown(self):
        self.finish_anna()
        self.teacher.post(f'/sessions/{self.session_id}/close', headers={'X-CSRFToken': self.csrf})
        again = self.join('Анна', 'Петрова', ANNA)
        with again.session_transaction() as s:
            s['student_id'] = self.anna_id
        self.assertIn('Правильный', again.get('/test/result', environ_base=ANNA).get_data(as_text=True))

    def test_monitor_marks_finished_and_teacher_only(self):
        self.finish_anna()
        live = self.teacher.get(f'/sessions/{self.session_id}/live').get_json()
        finished = {s['name']: s['finished'] for s in live['students']}
        self.assertEqual(finished, {'Петрова Анна': True, 'Сидоров Олег': False})
        # второй раз — уже нечего завершать
        self.assertFalse(self.finish_anna().get_json()['ok'])
        # только с компьютера учителя
        self.assertEqual(self.anna.post(f'/sessions/{self.session_id}/students/{self.oleg_id}/finish',
                                        environ_base=ANNA).status_code, 403)

    def test_results_page_has_button_while_running(self):
        page = self.teacher.get(f'/results?session_id={self.session_id}').get_data(as_text=True)
        self.assertEqual(page.count('⏹ Завершить</button>'), 2)
        self.finish_anna()
        page = self.teacher.get(f'/results?session_id={self.session_id}').get_data(as_text=True)
        self.assertEqual(page.count('⏹ Завершить</button>'), 1)
        self.assertIn('⏹ завершён', page)


if __name__ == '__main__':
    unittest.main()
