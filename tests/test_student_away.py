"""Уходы ученика со страницы теста: сколько раз и сколько секунд, и показ в результатах.

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
from models import Student, Task, TestSession, Variant, get_db  # noqa: E402
from results_logic import format_away  # noqa: E402

PC = {'REMOTE_ADDR': '192.168.1.21'}


class AwayTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        variant = Variant.create('Уходы', 'uploaded')
        Variant.add_tasks(variant, [task])
        self.session_id = TestSession.create(variant, 0, 5, None, 1, 1, 0, 0)
        self.pupil = server.app.test_client()
        self.pupil.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}, environ_base=PC)
        self.student_id = Student.get_by_session_and_name(self.session_id, 'Анна', 'Петрова')['id']

    def away(self, value):
        return self.pupil.post('/test/away', data={'away': value}, environ_base=PC)

    def row(self):
        return Student.get_by_id(self.student_id)

    def test_leave_and_return_are_counted(self):
        for _ in range(2):
            self.assertEqual(self.away('1').status_code, 204)
            self.assertEqual(self.away('0').status_code, 204)
        row = self.row()
        self.assertEqual((row['leave_count'], row['left_at']), (2, None))

    def test_repeated_leave_without_return_counts_once(self):
        self.away('1')
        self.away('1')
        self.assertEqual(self.row()['leave_count'], 1)

    def test_time_away_is_measured_by_server(self):
        self.away('1')
        conn = get_db()
        conn.execute("UPDATE students SET left_at = datetime('now', '-90 seconds') WHERE id = ?", (self.student_id,))
        conn.commit()
        conn.close()
        self.away('0')
        self.assertGreaterEqual(self.row()['leave_seconds'], 90)

    def test_finish_closes_open_absence(self):
        self.away('1')
        Student.finish(self.student_id)
        row = self.row()
        self.assertIsNone(row['left_at'])
        self.assertEqual(row['leave_count'], 1)

    def test_not_counted_after_submit(self):
        self.pupil.post('/test/submit', environ_base=PC)
        self.away('1')
        self.assertEqual(self.row()['leave_count'], 0)

    def test_format_and_results_page(self):
        self.assertEqual(format_away({'leave_count': 0}), '—')
        self.assertEqual(format_away({'leave_count': 3, 'leave_seconds': 130}), '3 раза · 2 мин 10 с')
        self.away('1')
        self.away('0')
        teacher = server.app.test_client()
        page = teacher.get(f'/results?session_id={self.session_id}').get_data(as_text=True)
        self.assertIn('Уходы', page)
        self.assertIn('1 раз ·', page)
        self.assertIn('Уходы со страницы теста', teacher.get(f'/results/student/{self.student_id}').get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
