"""Один ответ на пару «ученик — задание»: повторное сохранение не создаёт вторую запись
и не раздувает счёт (раньше при двух одновременных сохранениях верный ответ считался дважды).

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import server  # noqa: E402
from models import Answer, Student, Task, TestSession, Variant, get_db  # noqa: E402


class UniqueAnswerTests(unittest.TestCase):
    def tearDown(self):
        # не оставляем запущенное тестирование соседним тестам
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        variant = Variant.create('Уникальность', 'uploaded')
        Variant.add_tasks(variant, [self.task])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 0, 0)
        self.student_id = Student.create(self.session_id, 'Анна', 'Петрова', variant)

    def rows(self):
        conn = get_db()
        count = conn.execute('SELECT COUNT(*) FROM answers WHERE student_id = ? AND task_id = ?',
                             (self.student_id, self.task)).fetchone()[0]
        conn.close()
        return count

    def test_repeated_saves_keep_one_row(self):
        for value in ('7', '8', '7', '7'):
            Answer.save(self.student_id, self.task, answer_1=value)
        self.assertEqual(self.rows(), 1)
        self.assertTrue(Answer.get_for_student_task(self.student_id, self.task)['is_correct'])

    def test_database_refuses_a_second_row(self):
        Answer.save(self.student_id, self.task, answer_1='7')
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO answers (student_id, task_id, answer_1, is_correct) VALUES (?, ?, 7, 1)',
                         (self.student_id, self.task))
        conn.close()

    def test_correct_count_ignores_nothing_extra(self):
        Answer.save(self.student_id, self.task, answer_1='8')
        Answer.save(self.student_id, self.task, answer_1='7')
        Answer.save(self.student_id, self.task, answer_1='8')
        answers = Student.get_answers(self.student_id)
        self.assertEqual(len(answers), 1)
        self.assertFalse(answers[0]['is_correct'])


if __name__ == '__main__':
    unittest.main()
