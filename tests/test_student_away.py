"""Честность: уходы со страницы теста (в результатах и в мониторинге), время на каждое задание,
одинаковые неверные ответы у пары учеников.

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
import results_logic  # noqa: E402
from results_logic import format_away  # noqa: E402

PC = {'REMOTE_ADDR': '192.168.1.21'}
PC_2 = {'REMOTE_ADDR': '192.168.1.22'}
PC_3 = {'REMOTE_ADDR': '192.168.1.23'}


class AwayTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.tasks = [Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1=str(n)) for n in (7, 8, 9)]
        self.task = self.tasks[0]
        variant = Variant.create('Уходы', 'uploaded')
        Variant.add_tasks(variant, self.tasks)
        self.session_id = TestSession.create(variant, 0, 5, None, 1, 1, 0, 0)
        self.pupil = server.app.test_client()
        self.pupil.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}, environ_base=PC)
        self.student_id = Student.get_by_session_and_name(self.session_id, 'Анна', 'Петрова')['id']

    def away(self, value=None, times=None):
        data = {}
        if value is not None:
            data['away'] = value
        if times is not None:
            data['times'] = times
        return self.pupil.post('/test/away', data=data, environ_base=PC)

    def pupil_with_answers(self, pc, first, last, answers):
        client = server.app.test_client()
        client.post('/test/start', data={'first_name': first, 'last_name': last}, environ_base=pc)
        for task_id, answer in zip(self.tasks, answers):
            client.post('/test/save', json={'task_id': task_id, 'answer_1': answer}, environ_base=pc)
        return Student.get_by_session_and_name(self.session_id, first, last)['id']

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


    # ---- мониторинг: «вне страницы» сейчас и число уходов
    def test_monitor_shows_away_now(self):
        self.away('1')
        live = server.app.test_client().get(f'/sessions/{self.session_id}/live').get_json()
        (pupil,) = live['students']
        self.assertTrue(pupil['away'])
        self.assertEqual((pupil['leaves'], live['away']), (1, 1))
        self.away('0')
        live = server.app.test_client().get(f'/sessions/{self.session_id}/live').get_json()
        self.assertFalse(live['students'][0]['away'])
        self.assertEqual(live['students'][0]['leaves'], 1)

    # ---- время на каждое задание
    def test_task_time_is_summed(self):
        self.away(times='{"%d": 20, "%d": 5}' % (self.tasks[0], self.tasks[1]))
        self.away(times='{"%d": 10}' % self.tasks[0])
        self.assertEqual(Student.get_task_times(self.student_id), {self.tasks[0]: 30, self.tasks[1]: 5})

    def test_task_time_only_own_tasks_and_at_most_a_minute(self):
        other = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='1')
        self.away(times='{"%d": 50, "%d": 50, "%d": 50}' % (other, self.tasks[0], self.tasks[1]))
        self.assertEqual(Student.get_task_times(self.student_id), {self.tasks[0]: 50, self.tasks[1]: 10})
        self.away(times='не json')
        self.away(times='{"%d": -5}' % self.tasks[0])
        self.assertEqual(Student.get_task_times(self.student_id)[self.tasks[0]], 50)

    def test_task_time_not_counted_on_pause(self):
        TestSession.pause(self.session_id)
        self.away(times='{"%d": 20}' % self.tasks[0])
        self.assertEqual(Student.get_task_times(self.student_id), {})

    def test_fast_correct_answer_is_marked(self):
        self.pupil.post('/test/save', json={'task_id': self.tasks[0], 'answer_1': '7'}, environ_base=PC)
        self.pupil.post('/test/save', json={'task_id': self.tasks[1], 'answer_1': '8'}, environ_base=PC)
        self.away(times='{"%d": 4, "%d": 40}' % (self.tasks[0], self.tasks[1]))
        cells = results_logic.student_score(self.row())['cells']
        self.assertEqual([(c['time'], c['fast']) for c in cells], [('4 с', True), ('40 с', False), ('', False)])
        page = server.app.test_client().get(f'/results/student/{self.student_id}').get_data(as_text=True)
        self.assertIn('40 с', page)

    # ---- одинаковые неверные ответы
    def test_same_wrong_answers_are_found(self):
        first = self.pupil_with_answers(PC_2, 'Иван', 'Сидоров', ['1', '2', '9'])
        second = self.pupil_with_answers(PC_3, 'Пётр', 'Козлов', ['1', '2', '5'])
        _, summary = results_logic.session_results(TestSession.get_by_id(self.session_id))
        (match,) = summary['matches']
        self.assertEqual({s['id'] for s in match['students']}, {first, second})
        self.assertEqual(match['numbers'], [1, 2])
        teacher = server.app.test_client()
        self.assertIn('Одинаковые неверные ответы', teacher.get(f'/results?session_id={self.session_id}').get_data(as_text=True))
        self.assertIn('Козлов', teacher.get(f'/results/student/{first}').get_data(as_text=True))

    def test_one_same_wrong_answer_is_not_enough(self):
        self.pupil_with_answers(PC_2, 'Иван', 'Сидоров', ['1', '8', '9'])
        self.pupil_with_answers(PC_3, 'Пётр', 'Козлов', ['1', '2', '5'])
        _, summary = results_logic.session_results(TestSession.get_by_id(self.session_id))
        self.assertEqual(summary['matches'], [])

    def test_common_mistake_is_not_a_match(self):
        pcs = [{'REMOTE_ADDR': f'192.168.1.{30 + n}'} for n in range(4)]
        for n, pc in enumerate(pcs):
            self.pupil_with_answers(pc, f'Ученик{n}', 'Общий', ['1', '2', '9'])
        _, summary = results_logic.session_results(TestSession.get_by_id(self.session_id))
        self.assertEqual(summary['matches'], [])


if __name__ == '__main__':
    unittest.main()
