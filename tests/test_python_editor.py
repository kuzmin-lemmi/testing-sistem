"""Редактор Python под заданием: в программе ученика (student_app) — под каждым заданием,
в обычном браузере — прежняя боковая панель.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import json
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

PC = {'REMOTE_ADDR': '192.168.1.31'}
APP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Edg/130.0 TestingStudentApp/0.1'
BROWSER_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Edg/130.0'


class PythonEditorTests(unittest.TestCase):
    def start(self, python_enabled=1):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.plain = Task.create(ege_number=1, image_path='x.png', answer_1='5')
        self.with_file = Task.create(ege_number=24, image_path='y.png', answer_1='7',
                                     attachment_path='abc123.txt', attachment_name='24.txt')
        variant = Variant.create('Python под заданием', 'uploaded')
        Variant.add_tasks(variant, [self.plain, self.with_file])
        TestSession.create(variant, 0, 30, None, 1, 0, 0, python_enabled)

    def exam_page(self, user_agent):
        client = server.app.test_client()
        headers = {'User-Agent': user_agent}
        client.post('/test/start', data={'first_name': 'Иван', 'last_name': 'Орлов'}, environ_base=PC, headers=headers)
        return client.get('/test/exam', environ_base=PC, headers=headers).get_data(as_text=True)

    def test_student_app_gets_editor_under_each_task(self):
        self.start()
        page = self.exam_page(APP_UA)
        self.assertIn(f'id="py-task-{self.plain}"', page)
        self.assertIn(f'id="py-task-{self.with_file}"', page)
        self.assertIn('js/vendor/codemirror.min.js', page)
        self.assertIn('js/py_editor.js', page)
        self.assertNotIn('id="pyPanel"', page)  # боковой панели и кнопки в шапке нет
        self.assertNotIn('onclick="togglePython()"', page)
        self.assertIn('const pythonEnabled = false', page)

    def test_editor_comes_right_after_the_question(self):
        self.start()
        page = self.exam_page(APP_UA)
        card = page[page.index(f'data-task-id="{self.with_file}"'):]
        self.assertLess(card.index('class="task-image"'), card.index('class="py-task"'))
        self.assertLess(card.index('class="py-task"'), card.index('class="answer-section"'))
        self.assertIn("open('24.txt')", card[:card.index('class="answer-section"')])

    def test_task_files_are_passed_to_the_editor(self):
        self.start()
        page = self.exam_page(APP_UA)
        config = re.search(r'window\.PY_TASK_CONFIG = (\{.*\});', page).group(1)
        files = json.loads(config.split('files: ', 1)[1][:-1])
        self.assertEqual(files,{str(self.with_file): {'url': '/attachments/abc123.txt', 'name': '24.txt'}})

    def test_student_app_gets_split_layout(self):
        self.start()
        page = self.exam_page(APP_UA)
        self.assertIn('app-layout', page[page.index('<body'):page.index('<body') + 80])
        self.assertIn('css/exam_app.css', page)
        self.assertIn('js/exam_app.js', page)
        header = page[page.index('<header'):page.index('</header>')]
        self.assertIn('class="task-nav"', header)  # номера заданий — в шапке
        self.assertIn('data-task="1"', header)
        self.assertNotIn('class="sidebar"', page)
        card = page[page.index(f'data-task-id="{self.plain}"'):]
        self.assertIn('class="task-card with-py"', page)
        self.assertLess(card.index('class="task-text"'), card.index('class="py-task"'))
        self.assertLess(card.index('class="py-task"'), card.index('class="task-answer"'))

    def test_student_app_can_open_task_file_in_office(self):
        self.start()
        page = self.exam_page(APP_UA)
        self.assertIn('js/office.js', page)
        # имя файла — для кнопки «Открыть в LibreOffice» и для LibreOffice (файл ляжет под этим именем)
        self.assertIn('href="/attachments/abc123.txt" class="attachment" data-name="24.txt"', page)
        self.assertNotIn('js/office.js', self.exam_page(BROWSER_UA))

    def test_split_layout_only_in_student_app(self):
        self.start()
        page = self.exam_page(BROWSER_UA)
        self.assertNotIn('app-layout', page[page.index('<body'):page.index('<body') + 80])
        self.assertNotIn('exam_app', page)
        self.assertIn('class="sidebar"', page)
        self.assertNotIn('class="task-nav"', page)
        self.assertNotIn('with-py', page)

    def test_browser_keeps_side_panel(self):
        self.start()
        page = self.exam_page(BROWSER_UA)
        self.assertIn('id="pyPanel"', page)
        self.assertIn('const pythonEnabled = true', page)
        self.assertNotIn('class="py-task"', page)
        self.assertNotIn('py_editor.js', page)

    def test_no_editor_when_python_is_off(self):
        self.start(python_enabled=0)
        page = self.exam_page(APP_UA)
        self.assertNotIn('class="py-task"', page)
        self.assertNotIn('codemirror.min.js', page)

    @unittest.skipUnless(shutil.which('node'), 'Node.js не установлен')
    def test_insert_answer_fills_all_fields(self):
        """«Вставить в ответ»: одно поле — последняя строка, несколько — по полям (строками или из одной строки)."""
        script = os.path.join(os.path.dirname(server.__file__), 'static', 'js', 'py_editor.js')
        cases = [
            (['1', '1024'], 1, ['1024']),
            (['6', '36'], 2, ['6', '36']),
            (['отладка', '6', '36'], 2, ['6', '36']),
            (['6 36'], 2, ['6', '36']),
            (['12, 13'], 2, ['12', '13']),
            (['12 13', '30 31'], 4, ['12', '13', '30', '31']),
            (['5'], 4, ['5']),
            (['12', '13', '14', '15'], 4, ['12', '13', '14', '15']),
        ]
        stub = ("globalThis.window = globalThis; globalThis.document = {querySelectorAll: () => []};"
                "globalThis.addEventListener = () => {};")
        check = 'console.log(JSON.stringify(%s.map(([l, n]) => window.studentPython.answerValues(l, n))));' % (
            json.dumps([[lines, count] for lines, count, _ in cases], ensure_ascii=False))
        with open(script, encoding='utf-8') as f:
            code = stub + f.read() + check
        result = subprocess.run([shutil.which('node'), '-e', code], capture_output=True, text=True, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [expected for _, _, expected in cases])

    def test_static_files_exist(self):
        root = os.path.join(os.path.dirname(server.__file__), 'static')
        for path in ('js/vendor/codemirror.min.js', 'js/py_editor.js', 'css/py_editor.css'):
            self.assertTrue(os.path.isfile(os.path.join(root, *path.split('/'))), path)


if __name__ == '__main__':
    unittest.main()
