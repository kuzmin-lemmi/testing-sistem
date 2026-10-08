"""Скрипты на страницах ученика и учителя без синтаксических ошибок.

Одна опечатка в скрипте страницы теста ломает всё: ответы не сохраняются, кнопки не работают.
Проверяем отрисованные страницы через `node --check` (если Node.js не установлен, тест пропускается).

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import server  # noqa: E402
from models import Task, TestSession, Variant  # noqa: E402

NODE = shutil.which('node')
INLINE = re.compile(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', re.S)


@unittest.skipUnless(NODE, 'Node.js не установлен')
class PageScriptTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        task = Task.create(ege_number=1, image_path='x.png', answer_count=1, answer_1='7')
        two = Task.create(ege_number=1, image_path='x.png', answer_count=2, answer_1='1', answer_2='2')
        variant = Variant.create('Скрипты', 'uploaded')
        Variant.add_tasks(variant, [task, two])
        self.session_id = TestSession.create(variant, 0, 20, None, 1, 1, 1, 0, mask_answers=True)

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def check(self, html, page):
        scripts = INLINE.findall(html)
        self.assertTrue(scripts, page)
        for number, code in enumerate(scripts, 1):
            with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, encoding='utf-8') as stream:
                stream.write(code)
            try:
                result = subprocess.run([NODE, '--check', stream.name], capture_output=True, text=True, timeout=60)
            finally:
                os.unlink(stream.name)
            self.assertEqual(result.returncode, 0, f'{page}, скрипт {number}:\n{result.stderr[:800]}')

    def test_student_pages(self):
        pupil = server.app.test_client()
        pupil.post('/test/start', data={'first_name': 'Анна', 'last_name': 'Петрова'}, environ_base={'REMOTE_ADDR': '192.168.0.50'})
        self.check(pupil.get('/test/exam', environ_base={'REMOTE_ADDR': '192.168.0.50'}).get_data(as_text=True), 'страница теста')
        pupil.post('/test/submit', environ_base={'REMOTE_ADDR': '192.168.0.50'})
        self.check(pupil.get('/test/exam', environ_base={'REMOTE_ADDR': '192.168.0.50'}).get_data(as_text=True), 'заставка «Работа сдана»')

    def test_teacher_pages(self):
        teacher = server.app.test_client()
        for url in ('/', f'/sessions/{self.session_id}/monitor', '/variants'):
            self.check(teacher.get(url).get_data(as_text=True), url)


if __name__ == '__main__':
    unittest.main()
