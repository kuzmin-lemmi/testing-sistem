"""Страницы учителя: мониторинг, результаты (таблица, Excel, ручные отметки), варианты (правка),
запуск (оценки в процентах, запоминание), настройки и копии базы.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

if 'EGE_DATA_DIR' not in os.environ:
    os.environ['EGE_DATA_DIR'] = tempfile.mkdtemp(prefix='ege-test-')
    atexit.register(shutil.rmtree, os.environ['EGE_DATA_DIR'], True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import config  # noqa: E402
import results_logic  # noqa: E402
import server  # noqa: E402
from models import (Student, Task, TestSession, Variant, backup_database, get_setting, list_backups,  # noqa: E402
                    set_setting)

PC = {'REMOTE_ADDR': '192.168.1.40'}


def close_all_sessions():
    for item in TestSession.get_all():
        if item['status'] == 'active':
            TestSession.close(item['id'])


def make_variant(name='Страницы'):
    tasks = [
        Task.create(ege_number=1, image_path='a.png', answer_count=1, answer_1='42', task_scope='class'),
        Task.create(ege_number=1, image_path='b.png', answer_count=3, answer_1='1', answer_2='2', answer_text='3',
                    task_scope='class'),
        Task.create(ege_number=1, image_path='c.png', answer_count=0, answer_text='5 10\n7 3', task_scope='class'),
    ]
    variant_id = Variant.create(name, 'uploaded')
    Variant.add_tasks(variant_id, tasks)
    return variant_id, tasks


class Base(unittest.TestCase):
    def setUp(self):
        close_all_sessions()
        self.client = server.app.test_client()
        self.client.get('/settings')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']
        self.variant_id, self.tasks = make_variant()

    def post(self, url, **data):
        return self.client.post(url, data={'csrf_token': self.token, **data})

    def run_test(self, answers_by_student):
        session_id = TestSession.create(self.variant_id, 0, 45, None, 1, 1, 0, 0,
                                        grade_5_min=3, grade_4_min=2, grade_3_min=1, total_tasks=3)
        for number, (name, answers) in enumerate(answers_by_student.items()):
            pupil = server.app.test_client()
            env = {'REMOTE_ADDR': f'192.168.1.{50 + number}'}
            pupil.post('/test/start', data={'first_name': name, 'last_name': 'Тест'}, environ_base=env)
            for task_id, payload in zip(self.tasks, answers):
                if payload:
                    pupil.post('/test/save', json={'task_id': task_id, **payload}, environ_base=env)
        return session_id


class AnswerFormatTests(unittest.TestCase):
    def test_every_kind_of_answer_is_shown(self):
        one = {'answer_count': 1, 'answer_1': 12}
        three = {'answer_count': 3, 'answer_1': '1', 'answer_2': '2', 'answer_text': '3'}
        text = {'answer_count': 0, 'answer_text': '5 10\n7 3'}
        upload = {'answer_count': 0, 'answer_kind': 'file_upload'}
        self.assertEqual(results_logic.format_expected(one), '12')
        self.assertEqual(results_logic.format_expected(three), '1, 2, 3')
        self.assertEqual(results_logic.format_expected(text), '5 10 / 7 3')  # раньше «None»
        self.assertEqual(results_logic.format_expected(upload), 'проверяется вручную')
        self.assertEqual(results_logic.format_given(text, {'answer_text': 'x\n\ny'}), 'x / y')
        self.assertEqual(results_logic.format_given(three, {'answer_1': '1', 'answer_2': None, 'answer_text': '3'}),
                         '1, —, 3')
        self.assertEqual(results_logic.format_given(one, None), '—')

    def test_plural(self):
        self.assertEqual([results_logic.tasks_word(n) for n in (1, 3, 5, 11, 21, 22)],
                         ['1 задание', '3 задания', '5 заданий', '11 заданий', '21 задание', '22 задания'])

    def test_grade_minimums_from_percents(self):
        self.assertEqual(results_logic.grade_minimums(3, (85, 65, 45)), (3, 2, 2))
        self.assertEqual(results_logic.grade_minimums(27, (85, 65, 45)), (23, 18, 13))
        self.assertEqual(results_logic.grade_minimums(1, (85, 65, 45)), (1, 1, 1))


class MonitorTests(Base):
    def test_sessions_page_goes_to_monitor_while_test_runs(self):
        session_id = self.run_test({'Анна': [{'answer_1': '42'}, None, None]})
        self.assertTrue(self.client.get('/sessions').headers['Location'].endswith(f'/sessions/{session_id}/monitor'))
        page = self.client.get(f'/sessions/{session_id}/monitor').get_data(as_text=True)
        self.assertIn('Завершить тестирование', page)
        self.assertIn('id="timer"', page)

    def test_live_state(self):
        session_id = self.run_test({'Анна': [{'answer_1': '42'}, {'answer_1': '1', 'answer_2': '2', 'answer_text': '3'}, None],
                                    'Борис': [None, None, None]})
        state = self.client.get(f'/sessions/{session_id}/live').get_json()
        rows = {s['name']: s for s in state['students']}
        self.assertEqual((rows['Тест Анна']['answered'], rows['Тест Анна']['total']), (2, 3))
        self.assertEqual(rows['Тест Борис']['answered'], 0)
        self.assertTrue(rows['Тест Анна']['online'])
        self.assertGreater(state['remaining'], 44 * 60)
        self.assertRegex(rows['Тест Анна']['started'], r'^\d\d:\d\d$')

    def test_start_time_is_local_not_utc(self):
        session_id = self.run_test({'Анна': [None, None, None]})
        student = TestSession.get_students(session_id)[0]
        state = self.client.get(f'/sessions/{session_id}/live').get_json()
        self.assertEqual(state['students'][0]['started'], server.to_local_dt(student['started_at'])[11:16])

    def test_close_opens_results_and_closed_monitor_redirects(self):
        session_id = self.run_test({'Анна': [None, None, None]})
        response = self.post(f'/sessions/{session_id}/close')
        self.assertIn(f'/results?session_id={session_id}', response.headers['Location'])
        self.assertIn('/results', self.client.get(f'/sessions/{session_id}/monitor').headers['Location'])

    def test_cannot_start_second_test_while_one_runs(self):
        session_id = self.run_test({})
        response = self.client.get('/sessions/new')
        self.assertTrue(response.headers['Location'].endswith(f'/sessions/{session_id}/monitor'))

    def test_no_active_test_page(self):
        page = self.client.get('/sessions').get_data(as_text=True)
        self.assertIn('Тестирование не запущено', page)
        self.assertNotIn('Удалить отмеченные', page)


class ResultsTests(Base):
    def setUp(self):
        super().setUp()
        self.session_id = self.run_test({
            'Анна': [{'answer_1': '42'}, {'answer_1': '1', 'answer_2': '2', 'answer_text': '3'}, {'answer_text': '5 10\n7 3'}],
            'Борис': [{'answer_1': '41'}, None, {'answer_text': 'x'}],
        })
        Student.finish_all(self.session_id)
        TestSession.close(self.session_id)

    def test_matrix(self):
        page = self.client.get(f'/results?session_id={self.session_id}').get_data(as_text=True)
        self.assertEqual(page.count('class="cell ok"'), 3)
        self.assertEqual(page.count('class="cell bad"'), 2)
        self.assertEqual(page.count('class="cell none"'), 1)
        self.assertIn('Решили', page)
        self.assertIn('/results/export/%d.xlsx' % self.session_id, page)

    def test_student_page_shows_all_answer_kinds(self):
        student = Student.get_by_session_and_name(self.session_id, 'Анна', 'Тест')
        page = self.client.get(f"/results/student/{student['id']}").get_data(as_text=True)
        self.assertIn('5 10 / 7 3', page)
        self.assertIn('1, 2, 3', page)
        self.assertNotIn('None', page)

    def test_manual_mark_changes_score(self):
        student = Student.get_by_session_and_name(self.session_id, 'Борис', 'Тест')
        self.assertEqual(results_logic.student_score(student)['correct'], 0)
        response = self.client.post('/teacher/answers/mark', headers={'X-CSRFToken': self.token},
                                    json={'student_id': student['id'], 'task_id': self.tasks[2], 'is_correct': True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(results_logic.student_score(student)['correct'], 1)

    def test_excel(self):
        response = self.client.get(f'/results/export/{self.session_id}.xlsx')
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response.headers['Content-Type'])
        with zipfile.ZipFile(io.BytesIO(response.get_data())) as archive:
            self.assertIsNone(archive.testzip())
            sheet = archive.read('xl/worksheets/sheet1.xml').decode('utf-8')
            answers = archive.read('xl/worksheets/sheet2.xml').decode('utf-8')
        self.assertIn('Тест Анна', sheet)
        self.assertIn('5 10 / 7 3', sheet)
        self.assertIn('Решили', sheet)
        self.assertIn('1, 2, 3', answers)

    def test_student_result_page_has_no_none(self):
        close_all_sessions()
        session_id = TestSession.create(self.variant_id, 0, 45, None, 1, 1, 0, 0,
                                        grade_5_min=3, grade_4_min=2, grade_3_min=1, total_tasks=3)
        pupil, env = server.app.test_client(), {'REMOTE_ADDR': '192.168.1.60'}
        pupil.post('/test/start', data={'first_name': 'Вера', 'last_name': 'Тест'}, environ_base=env)
        pupil.post('/test/save', json={'task_id': self.tasks[2], 'answer_text': '5 10' + chr(10) + '7 3'}, environ_base=env)
        Student.finish_all(session_id)
        TestSession.close(session_id)
        page = pupil.get('/test/result', environ_base=env).get_data(as_text=True)
        self.assertIn('5 10 / 7 3', page)
        self.assertNotIn('None', page)


class VariantEditingTests(Base):
    def test_rename(self):
        self.post(f'/variants/{self.variant_id}/rename', name='  Новое   имя ')
        self.assertEqual(Variant.get_by_id(self.variant_id)['name'], 'Новое имя')

    def test_fix_answer_rechecks_saved_answers(self):
        session_id = self.run_test({'Анна': [{'answer_1': '41'}, None, None]})
        student = TestSession.get_students(session_id)[0]
        self.assertEqual(results_logic.student_score(student)['correct'], 0)
        response = self.post(f'/variants/{self.variant_id}/tasks/{self.tasks[0]}/answer', answer='41')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(str(Task.get_by_id(self.tasks[0])['answer_1']), '41')
        self.assertEqual(results_logic.student_score(student)['correct'], 1)

    def test_fix_multi_field_and_text_answers(self):
        self.client.post(f'/variants/{self.variant_id}/tasks/{self.tasks[1]}/answer',
                         data={'csrf_token': self.token, 'answer': ['7', '8', '9']})
        self.assertEqual(results_logic.format_expected(Task.get_by_id(self.tasks[1])), '7, 8, 9')
        self.post(f'/variants/{self.variant_id}/tasks/{self.tasks[2]}/answer', answer_text='1 2\n3 4')
        self.assertEqual(Task.get_by_id(self.tasks[2])['answer_text'], '1 2\n3 4')
        self.post(f'/variants/{self.variant_id}/tasks/{self.tasks[0]}/answer', answer='')
        self.assertEqual(str(Task.get_by_id(self.tasks[0])['answer_1']), '42')  # пустой ответ не принимается

    def test_move_and_remove(self):
        self.post(f'/variants/{self.variant_id}/tasks/{self.tasks[2]}/move', direction='up')
        self.assertEqual([t['id'] for t in Variant.get_tasks(self.variant_id)], [self.tasks[0], self.tasks[2], self.tasks[1]])
        self.post(f'/variants/{self.variant_id}/tasks/{self.tasks[0]}/remove')
        self.assertEqual([t['id'] for t in Variant.get_tasks(self.variant_id)], [self.tasks[2], self.tasks[1]])
        self.assertEqual([t['position'] for t in Variant.get_tasks(self.variant_id)], [1, 2])
        self.assertIsNotNone(Task.get_by_id(self.tasks[0]))  # само задание в банке остаётся

    def test_task_of_another_variant_is_not_touched(self):
        other_variant, other_tasks = make_variant('Другой')
        self.post(f'/variants/{self.variant_id}/tasks/{other_tasks[0]}/remove')
        self.assertEqual(len(Variant.get_tasks(other_variant)), 3)

    def test_pages_show_usage_and_thumbnails(self):
        self.run_test({})
        page = self.client.get(f'/variants/{self.variant_id}').get_data(as_text=True)
        self.assertIn('проводился 1 раз', page)
        self.assertIn('5 10 / 7 3', page)
        listing = self.client.get(f'/variants?variant_id={self.variant_id}').get_data(as_text=True)
        self.assertIn('class="thumb"', listing)
        self.assertIn('3 задания', listing)


class LaunchTests(Base):
    def test_grades_are_computed_from_the_real_task_count_and_settings_remembered(self):
        response = self.post('/sessions/new', variant_mode='single', variant_id=str(self.variant_id), time_limit='30',
                             calculator_enabled='1', total_tasks='99', grade_5_min='50', grade_4_min='40', grade_3_min='30')
        active = TestSession.get_active()
        self.assertTrue(response.headers['Location'].endswith(f"/sessions/{active['id']}/monitor"))
        self.assertEqual((active['total_tasks'], active['grade_5_min'], active['grade_4_min'], active['grade_3_min']),
                         (3, 3, 3, 3))  # пороги не больше числа заданий
        self.assertEqual((get_setting('last_time_limit'), get_setting('last_calculator'), get_setting('last_python')),
                         ('30', '1', '0'))
        close_all_sessions()
        page = self.client.get('/sessions/new').get_data(as_text=True)
        self.assertIn('value="30"', page)

    def test_default_grades_from_percents(self):
        self.post('/sessions/new', variant_mode='single', variant_id=str(self.variant_id), time_limit='30')
        active = TestSession.get_active()
        self.assertEqual((active['grade_5_min'], active['grade_4_min'], active['grade_3_min']), (3, 2, 2))


class SettingsTests(Base):
    def tearDown(self):
        for key in ('grade_5_percent', 'grade_4_percent', 'grade_3_percent', 'show_ege_banks', 'show_access_code'):
            set_setting(key, None)

    def test_percents_and_toggles(self):
        self.post('/settings', grade_5_percent='90', grade_4_percent='70', grade_3_percent='50',
                  show_ege_banks='1', show_access_code='1')
        self.assertEqual(server.grade_percents(), (90, 70, 50))
        self.assertTrue(server.show_ege_banks())
        self.assertIn('Банк задач', self.client.get('/').get_data(as_text=True))
        self.assertIn('name="use_code"', self.client.get('/sessions/new').get_data(as_text=True))
        self.post('/settings', grade_5_percent='90', grade_4_percent='70', grade_3_percent='50')
        self.assertFalse(server.show_ege_banks())

    def test_bad_percents_are_refused(self):
        self.post('/settings', grade_5_percent='50', grade_4_percent='70', grade_3_percent='40')
        self.assertEqual(server.grade_percents(), server.DEFAULT_GRADE_PERCENTS)

    def test_backup(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        with patch('server.BACKUPS_DIR', folder):
            self.post('/settings/backup')
            page = self.client.get('/settings').get_data(as_text=True)
        (item,) = list_backups(folder)
        self.assertIn(item['name'], page)
        for _ in range(4):
            backup_database(folder, keep=3)
        self.assertEqual(len(list_backups(folder)), 3)


if __name__ == '__main__':
    unittest.main()
