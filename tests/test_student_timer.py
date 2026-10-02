"""Время у каждого ученика идёт с момента его входа, а не с запуска тестирования учителем.

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


def run_sql(sql, params=()):
    conn = get_db()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


class StudentTimerTests(unittest.TestCase):
    def tearDown(self):
        # не оставляем запущенное тестирование соседним тестам
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        variant = Variant.create('Таймер', 'uploaded')
        Variant.add_tasks(variant, [task])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 0, 0, 0)  # 20 минут
        # Тестирование запущено 10 минут назад
        run_sql("UPDATE test_sessions SET created_at = datetime('now', '-600 seconds') WHERE id = ?", (self.session_id,))

    def join(self, first='Анна', last='Петрова'):
        client = server.app.test_client()
        client.post('/test/start', data={'first_name': first, 'last_name': last})
        return Student.get_by_session_and_name(self.session_id, first, last)

    def remaining(self, student):
        return server._calc_remaining_seconds(Student.get_by_id(student['id']), TestSession.get_by_id(self.session_id))

    def test_late_student_gets_full_time(self):
        student = self.join()
        self.assertGreaterEqual(self.remaining(student), 20 * 60 - 2)

    def test_pause_before_join_does_not_add_time(self):
        # Пауза длиной 5 минут закончилась до входа ученика
        run_sql("UPDATE test_sessions SET pause_total_seconds = 300 WHERE id = ?", (self.session_id,))
        student = self.join()
        self.assertLessEqual(self.remaining(student), 20 * 60)
        run_sql("UPDATE students SET started_at = datetime('now', '-120 seconds') WHERE id = ?", (student['id'],))
        self.assertAlmostEqual(self.remaining(student), 18 * 60, delta=2)

    def test_pause_after_join_stops_time(self):
        student = self.join()
        run_sql("UPDATE students SET started_at = datetime('now', '-300 seconds') WHERE id = ?", (student['id'],))
        run_sql("UPDATE test_sessions SET paused = 1, paused_at = datetime('now', '-120 seconds') WHERE id = ?",
                (self.session_id,))
        self.assertAlmostEqual(self.remaining(student), 17 * 60, delta=2)

    def test_teacher_timer_waits_for_students(self):
        state = server._monitor_state(TestSession.get_by_id(self.session_id))
        self.assertTrue(state['waiting'])
        self.assertEqual(state['remaining'], 20 * 60)
        self.join()
        state = server._monitor_state(TestSession.get_by_id(self.session_id))
        self.assertFalse(state['waiting'])


if __name__ == '__main__':
    unittest.main()
