"""«Задания вразброс»: у каждого ученика свой порядок заданий, но он стабилен и ответы не путаются.

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

COUNT = 12


class ShuffleTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.task_ids = [Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1=str(i)) for i in range(COUNT)]
        self.variant = Variant.create('Вразброс', 'uploaded')
        Variant.add_tasks(self.variant, self.task_ids)

    def start(self, shuffle):
        session_id = TestSession.create(self.variant, 0, 20, None, 1, 1, 0, 0, shuffle_tasks=shuffle)
        return session_id

    def order(self, student):
        session = TestSession.get_by_id(student['session_id'])
        return [t['id'] for t in server._tasks_for_student(Student.get_by_id(student['id']), session)]

    def join(self, session_id, first):
        server.app.test_client().post('/test/start', data={'first_name': first, 'last_name': 'Тестов'})
        return Student.get_by_session_and_name(session_id, first, 'Тестов')

    def test_off_keeps_variant_order(self):
        sid = self.start(False)
        self.assertEqual(self.order(self.join(sid, 'Анна')), self.task_ids)

    def test_on_gives_each_student_own_stable_order(self):
        sid = self.start(True)
        students = [self.join(sid, name) for name in ('Анна', 'Борис', 'Вера', 'Глеб')]
        orders = [self.order(s) for s in students]
        for order in orders:
            self.assertEqual(sorted(order), sorted(self.task_ids))  # те же задания, ничего не потеряно
        self.assertGreater(len({tuple(o) for o in orders}), 1)       # порядки различаются
        self.assertEqual(orders, [self.order(s) for s in students])  # и не меняются при повторном запросе

    def test_exam_page_uses_student_order(self):
        sid = self.start(True)
        client = server.app.test_client()
        client.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Тестов'})
        student = Student.get_by_session_and_name(sid, 'Анна', 'Тестов')
        html = client.get('/test/exam').get_data(as_text=True)
        positions = [html.index(f'data-task-id="{task_id}"') for task_id in self.order(student)]
        self.assertEqual(positions, sorted(positions))

    def test_form_remembers_choice(self):
        client = server.app.test_client()
        self.assertIn('name="shuffle_tasks"', client.get('/sessions/new').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
