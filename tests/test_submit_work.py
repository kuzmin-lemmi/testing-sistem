"""«Работа сдана»: ученик сдаёт сам или учитель принимает работу; ответы скрыты и не меняются,
оценка — только после завершения тестирования; учитель может вернуть к решению. Плюс скрытие ответов точками.

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

PUPIL = {'REMOTE_ADDR': '192.168.0.31'}


class SubmitWorkTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.t1 = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        self.t2 = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='8')
        variant = Variant.create('Сдача', 'uploaded')
        Variant.add_tasks(variant, [self.t1, self.t2])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 0, 0, mask_answers=True)
        self.pupil = server.app.test_client()
        self.pupil.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}, environ_base=PUPIL)
        self.student = Student.get_by_session_and_name(self.session_id, 'Анна', 'Петрова')
        self.teacher = server.app.test_client()
        self.teacher.get('/')
        with self.teacher.session_transaction() as s:
            self.csrf = s['_csrf_token']

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def save(self, task, value):
        return self.pupil.post('/test/save', json={'task_id': task, 'answer_1': value}, environ_base=PUPIL)

    def exam(self):
        return self.pupil.get('/test/exam', environ_base=PUPIL).get_data(as_text=True)

    def teacher_post(self, url):
        return self.teacher.post(url, headers={'X-CSRFToken': self.csrf})

    def test_student_submits_and_sees_only_the_screen(self):
        self.assertEqual(self.save(self.t1, '7').status_code, 200)
        self.assertIn('Сдать работу', self.exam())
        self.pupil.post('/test/submit', environ_base=PUPIL)
        page = self.exam()
        self.assertIn('Работа сдана', page)
        self.assertNotIn('data-task-id', page)              # ни заданий, ни ответов
        self.assertEqual(self.save(self.t2, '8').status_code, 403)  # ответы больше не принимаются
        # оценка до завершения тестирования не показывается
        self.assertIn('Работа сдана', self.pupil.get('/test/result', follow_redirects=True, environ_base=PUPIL).get_data(as_text=True))
        # повторный вход не возвращает к заданиям
        again = server.app.test_client()
        again.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}, environ_base=PUPIL)
        self.assertNotIn('data-task-id', again.get('/test/exam', environ_base=PUPIL).get_data(as_text=True))

    def test_grade_appears_after_teacher_closes(self):
        self.save(self.t1, '7')
        self.pupil.post('/test/submit', environ_base=PUPIL)
        self.teacher_post(f'/sessions/{self.session_id}/close')
        # заставка опрашивает /test/ping и переходит на страницу с оценкой
        ping = self.pupil.post('/test/ping', environ_base=PUPIL).get_json()
        self.assertTrue(ping.get('redirect'))
        page = self.pupil.get(ping['redirect'], environ_base=PUPIL).get_data(as_text=True)
        self.assertIn('Оценка', page)

    def test_teacher_accepts_and_returns(self):
        self.assertTrue(self.teacher_post(f'/sessions/{self.session_id}/students/{self.student["id"]}/accept').get_json()['ok'])
        self.assertIn('Работа сдана', self.exam())
        ping = self.pupil.post('/test/ping', environ_base=PUPIL).get_json()
        self.assertTrue(ping['submitted'])
        self.teacher_post(f'/sessions/{self.session_id}/students/{self.student["id"]}/return')
        self.assertIn('data-task-id', self.exam())
        self.assertEqual(self.save(self.t1, '7').status_code, 200)

    def test_accept_all_who_answered_everything(self):
        other = server.app.test_client()
        other.post('/test/start', data={'first_name': 'Борис', 'last_name': 'Сидоров'}, environ_base={'REMOTE_ADDR': '192.168.0.32'})
        self.save(self.t1, '7'); self.save(self.t2, '1')       # Анна ответила на всё
        state = server._monitor_state(TestSession.get_by_id(self.session_id))
        self.assertEqual(state['ready_count'], 1)
        self.assertEqual(self.teacher_post(f'/sessions/{self.session_id}/accept-ready').get_json()['accepted'], 1)
        state = server._monitor_state(TestSession.get_by_id(self.session_id))
        self.assertEqual(state['submitted_count'], 1)
        borya = [s for s in state['students'] if s['name'].startswith('Сидоров')][0]
        self.assertFalse(borya['submitted'])

    def test_students_cannot_accept_or_return(self):
        url = f'/sessions/{self.session_id}/students/{self.student["id"]}/return'
        self.assertEqual(self.pupil.post(url, environ_base=PUPIL).status_code, 403)

    def test_mask_option(self):
        self.assertIn('class="mask-answers"', self.exam())
        page = server.app.test_client().get('/sessions/new')
        self.assertTrue(page.status_code in (200, 302))


if __name__ == '__main__':
    unittest.main()
