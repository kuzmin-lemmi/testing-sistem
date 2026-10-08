"""Ответ с русскими буквами, похожими на латинские («3В8» вместо «3B8»), засчитывается.

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

import server  # noqa: E402,F401
from models import Answer, Student, Task, TestSession, Variant, get_db, set_setting, recheck_wrong_answers_once  # noqa: E402


class LookalikeTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.hex_task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='3B8')
        self.ru_task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='БГДАВЗЖЕ')
        variant = Variant.create('Буквы', 'uploaded')
        Variant.add_tasks(variant, [self.hex_task, self.ru_task])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 0, 0)
        self.student = Student.create(self.session_id, 'Анна', 'Петрова', variant)

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def test_russian_letters_in_hex_answer(self):
        for typed in ('3В8', '3в8', '3b8', '3B8', ' 3В8 '):
            self.assertTrue(Answer.save(self.student, self.hex_task, answer_1=typed), typed)
        self.assertFalse(Answer.save(self.student, self.hex_task, answer_1='3В9'))

    def test_all_lookalikes(self):
        task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='ABCEHKMOPTXY')
        self.assertTrue(Answer.save(self.student, task, answer_1='АВСЕНКМОРТХУ'))

    def test_russian_word_answers_still_work(self):
        self.assertTrue(Answer.save(self.student, self.ru_task, answer_1='бгдавзже'))
        self.assertFalse(Answer.save(self.student, self.ru_task, answer_1='БГДАВЗЖИ'))

    def test_old_wrong_answers_are_rechecked_once(self):
        conn = get_db()
        conn.execute("INSERT INTO answers (student_id, task_id, answer_1, is_correct, answered_at) "
                     "VALUES (?, ?, '3В8', 0, '2026-01-01 10:00:00')", (self.student, self.hex_task))
        conn.commit(); conn.close()
        set_setting('lookalike_recheck_done', '0')
        self.assertGreaterEqual(recheck_wrong_answers_once(), 1)
        answer = Answer.get_for_student_task(self.student, self.hex_task)
        self.assertTrue(answer['is_correct'])
        self.assertEqual(answer['answered_at'], '2026-01-01 10:00:00')  # время ответа не изменилось
        self.assertEqual(recheck_wrong_answers_once(), 0)  # второй раз не запускается


if __name__ == '__main__':
    unittest.main()
