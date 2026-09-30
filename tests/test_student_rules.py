"""Правила для ученика: ответы принимаются только пока идёт время и тест не завершён учителем,
считаются только задания своего варианта, чужое имя занять нельзя, завершает тест только учитель.

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

import config  # noqa: E402
import server  # noqa: E402
from models import Student, Task, TestSession, Variant, get_db  # noqa: E402

PC_1 = {'REMOTE_ADDR': '192.168.1.11'}
PC_2 = {'REMOTE_ADDR': '192.168.1.12'}


def close_all_sessions():
    for item in TestSession.get_all():
        if item['status'] == 'active':
            TestSession.close(item['id'])


def shift_student(student_id, column, seconds_ago):
    conn = get_db()
    conn.execute(f"UPDATE students SET {column} = datetime('now', ?) WHERE id = ?", (f'-{seconds_ago} seconds', student_id))
    conn.commit()
    conn.close()


class RulesTests(unittest.TestCase):
    def setUp(self):
        close_all_sessions()
        self.task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        self.other_task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='8')
        self.variant = Variant.create('Правила', 'uploaded')
        Variant.add_tasks(self.variant, [self.task])
        self.session_id = TestSession.create(self.variant, 0, 5, None, 1, 1, 0, 0)  # 5 минут, завершает учитель
        self.pupil = server.app.test_client()
        self.assertEqual(self.join(self.pupil, PC_1).status_code, 302)
        self.student = Student.get_by_session_and_name(self.session_id, 'Анна', 'Петрова')

    def join(self, client, pc, first='Анна', last='Петрова'):
        return client.post('/test/start', data={'first_name': first, 'last_name': last}, environ_base=pc)

    def save(self, task_id=None, answer='7', raw=None):
        if raw is not None:
            return self.pupil.post('/test/save', data=raw, content_type='application/json', environ_base=PC_1)
        return self.pupil.post('/test/save', json={'task_id': task_id or self.task, 'answer_1': answer},
                               environ_base=PC_1)

    # ---- пока идёт время, ответы можно менять сколько угодно
    def test_answers_can_be_changed_while_time_runs(self):
        for answer in ('1', '2', '7'):
            self.assertEqual(self.save(answer=answer).status_code, 200)
        (answer,) = Student.get_answers(self.student['id'])
        self.assertEqual((str(answer['answer_1']), answer['is_correct']), ('7', 1))

    # ---- 2. ответ не принимается
    def test_refused_after_time_is_over(self):
        shift_student(self.student['id'], 'started_at', 5 * 60 + 60)
        response = self.save()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()['error'], 'Время вышло')

    def test_last_answers_sent_right_at_the_end_are_still_accepted(self):
        shift_student(self.student['id'], 'started_at', 5 * 60 + 3)
        self.assertEqual(self.save().status_code, 200)

    def test_extra_time_from_teacher_counts(self):
        shift_student(self.student['id'], 'started_at', 5 * 60 + 60)
        TestSession.extend_time(self.session_id, 5 * 60)
        self.assertEqual(self.save().status_code, 200)

    def test_refused_while_paused(self):
        TestSession.pause(self.session_id)
        self.assertEqual(self.save().get_json()['error'], 'Тестирование на паузе')
        TestSession.resume(self.session_id)
        self.assertEqual(self.save().status_code, 200)

    def test_refused_after_teacher_closed_the_test(self):
        Student.finish_all(self.session_id)
        TestSession.close(self.session_id)
        self.assertEqual(self.save().status_code, 403)

    # ---- 3, 4. только задания своего варианта
    def test_task_outside_the_variant_is_refused_and_not_counted(self):
        self.assertEqual(self.save(task_id=self.other_task, answer='8').status_code, 403)
        self.assertEqual(self.save(task_id=999999).status_code, 403)  # раньше: ошибка 500
        self.assertEqual(self.save(raw='не json').status_code, 400)
        self.assertEqual(self.save(raw='{"task_id": "abc"}').status_code, 400)

    def test_result_counts_only_tasks_of_the_variant(self):
        self.save()
        # ответ на задание, которое потом убрали из варианта, в счёт не идёт («2 из 1» больше не будет)
        conn = get_db()
        conn.execute('INSERT INTO answers (student_id, task_id, answer_1, is_correct) VALUES (?, ?, ?, 1)',
                     (self.student['id'], self.other_task, '8'))
        conn.commit()
        conn.close()
        self.assertEqual([a['task_id'] for a in Student.get_answers(self.student['id'])], [self.task])
        page = server.app.test_client().get(f"/results/student/{self.student['id']}").get_data(as_text=True)
        self.assertIn('<div class="big">1 <span class="muted">из 1</span></div>', page)

    # ---- 6. чужое имя
    def test_cannot_take_over_a_classmate_who_is_working(self):
        neighbour = server.app.test_client()
        response = self.join(neighbour, PC_2)
        self.assertEqual(response.status_code, 302)
        page = neighbour.get('/test', environ_base=PC_2).get_data(as_text=True)
        self.assertIn('уже решает тест на другом компьютере', page)
        with neighbour.session_transaction() as session:
            self.assertNotIn('student_id', session)

    def test_same_browser_can_come_back(self):
        self.assertEqual(self.join(self.pupil, PC_1).headers['Location'], '/test/exam')

    def test_can_continue_on_another_computer_after_the_old_window_is_closed(self):
        shift_student(self.student['id'], 'last_seen_at', server.RELOGIN_BLOCK_SECONDS + 5)
        other = server.app.test_client()
        self.assertEqual(self.join(other, PC_2).headers['Location'], '/test/exam')

    # ---- завершает только учитель
    def test_student_cannot_finish_or_see_results_before_the_teacher(self):
        self.assertEqual(self.pupil.post('/test/finish', environ_base=PC_1).headers['Location'], '/test/exam')
        self.assertEqual(self.pupil.get('/test/result', environ_base=PC_1).headers['Location'], '/test/exam')
        self.assertEqual(Student.get_by_id(self.student['id'])['status'], 'in_progress')

    def test_everyone_sees_grade_and_answers_after_teacher_closes(self):
        self.save()
        Student.finish_all(self.session_id)
        TestSession.close(self.session_id)
        page = self.pupil.get('/test/result', environ_base=PC_1).get_data(as_text=True)
        self.assertIn('data-watch="result"', page)
        self.assertIn('7', page)


class TeacherFormTests(unittest.TestCase):
    def setUp(self):
        close_all_sessions()
        self.client = server.app.test_client()
        self.client.get('/sessions/new')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']

    def test_every_new_test_is_finished_by_the_teacher_only(self):
        page = self.client.get('/sessions/new').get_data(as_text=True)
        self.assertNotIn('name="teacher_finish_only"', page)
        self.assertNotIn('Показывать правильные ответы', page)
        variant = Variant.create('Форма', 'uploaded')
        Variant.add_tasks(variant, [Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='1')])
        self.client.post('/sessions/new', data={'csrf_token': self.token, 'variant_mode': 'single',
                                                'variant_id': str(variant), 'time_limit': '45', 'show_answers': '1',
                                                'total_tasks': '1', 'grade_5_min': '1', 'grade_4_min': '1',
                                                'grade_3_min': '1'})
        active = TestSession.get_active()
        self.assertEqual((active['teacher_finish_only'], active['show_answers']), (1, 1))


class SecretKeyTests(unittest.TestCase):
    def test_own_random_key_is_created_and_kept(self):
        self.assertNotEqual(server.app.secret_key, 'ege-local-secret-change-me')
        self.assertGreaterEqual(len(server.app.secret_key), 32)
        with open(os.path.join(config.DATA_DIR, 'secret_key'), encoding='utf-8') as stream:
            self.assertEqual(stream.read().strip(), server.app.secret_key)
        self.assertEqual(config._load_secret_key(), server.app.secret_key)  # при следующем запуске тот же


if __name__ == '__main__':
    unittest.main()
