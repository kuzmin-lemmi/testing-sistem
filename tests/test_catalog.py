"""Каталог вариантов «Класс → Тема → Комплект»: угадывание по названию, «паспорт» в файле .variant,
загрузка нескольких файлов, перекладывание, запуск комплекта, средний результат.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import catalog  # noqa: E402
import server  # noqa: E402
from models import Answer, Student, Task, TestSession, Variant, autofill_catalog_once, get_db, set_setting  # noqa: E402

PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
       b'\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


def variant_file(name, answers=('1', '2'), passport=None):
    buffer = io.BytesIO()
    tasks = [{'id': f'abcdef{i:02d}', 'mode': 'fields', 'answers': [a]} for i, a in enumerate(answers)]
    manifest = {'version': 1, 'name': name, 'tasks': tasks}
    if passport is not None:
        manifest['catalog'] = passport
    with zipfile.ZipFile(buffer, 'w') as archive:
        archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False))
        for task in tasks:
            archive.writestr(f'images/{task["id"]}.png', PNG)
    buffer.seek(0)
    return buffer


def teacher():
    client = server.app.test_client()
    client.get('/')
    with client.session_transaction() as s:
        token = s['_csrf_token']
    return client, token


def by_name(name):
    return next(v for v in Variant.get_all() if v['name'] == name)


class GuessTests(unittest.TestCase):
    def test_names(self):
        g = catalog.guess('Алфавитный подход (7 класс, 20 минут) - вариант 3')
        self.assertEqual((g['grade'], g['topic'], g['kit'], g['minutes']),
                         ('7 класс', 'Алфавитный подход', 'Алфавитный подход (7 класс, 20 минут)', 20))
        self.assertEqual(catalog.guess('Системы счисления - сложный (40 минут)')['topic'], 'Системы счисления')
        self.assertEqual(catalog.guess('Системы счисления - сложный (40 минут)')['kit'], '')  # без «вариант N» — не комплект
        self.assertEqual(catalog.guess('10Б_29_09')['grade'], '10 класс')
        self.assertEqual(catalog.guess('7б')['grade'], '7 класс')
        self.assertEqual(catalog.guess('ЕГЭ №1, 2, 4 (kompege)')['grade'], 'ЕГЭ')
        self.assertEqual(catalog.guess('ОГЭ вариант 1')['grade'], '9 класс')

    def test_first_variant_joins_its_kit(self):
        items = [{'name': 'Тема (20 мин)', 'kit': ''}, {'name': 'Тема (20 мин) - вариант 2', 'kit': 'Тема (20 мин)'}]
        catalog.join_kits(items)
        self.assertEqual(items[0]['kit'], 'Тема (20 мин)')

    def test_passport_wins_over_name(self):
        place = catalog.from_manifest({'grade': '8', 'topic': 'Логика', 'kit': 'Проверочная', 'minutes': 25}, 'что угодно')
        self.assertEqual(place, {'grade': '8 класс', 'topic': 'Логика', 'kit': 'Проверочная', 'minutes': 25})

    def test_tree(self):
        rows = [
            {'id': 1, 'name': 'А', 'grade': '10 класс', 'topic': 'СС', 'kit': 'К', 'tasks_count': 5, 'minutes': 40},
            {'id': 2, 'name': 'А - вариант 2', 'grade': '10 класс', 'topic': 'СС', 'kit': 'К', 'tasks_count': 5},
            {'id': 3, 'name': 'Б', 'grade': '7 класс', 'topic': '', 'kit': '', 'tasks_count': 3},
            {'id': 4, 'name': 'В', 'grade': '', 'topic': '', 'kit': '', 'tasks_count': 3},
        ]
        tree = catalog.build_tree(rows)
        self.assertEqual([g['title'] for g in tree], ['7 класс', '10 класс', catalog.NO_GRADE])
        kit = tree[1]['topics'][0]['kits'][0]
        self.assertTrue(kit['is_kit'] and kit['launchable'])
        self.assertEqual(kit['minutes'], 40)
        self.assertFalse(tree[0]['topics'][0]['kits'][0]['is_kit'])


class WorksListTests(unittest.TestCase):
    """Список «работ» на странице вариантов: комплект — одна строка с переключателями В1…Вn."""

    def test_variant_numbers(self):
        self.assertEqual(catalog.variant_no('11 кл · ЕГЭ сборный · №1, 2, 4 · средний · В3'), 3)
        self.assertEqual(catalog.variant_no('8 кл · Системы счисления · сложение · В2 (простой)'), 2)
        self.assertEqual(catalog.variant_no('Алфавитный подход (7 класс) - вариант 4'), 4)
        self.assertIsNone(catalog.variant_no('11 кл · ЕГЭ №17 · пары и тройки'))

    def test_kit_is_one_work_newest_first(self):
        common = {'grade': '11 класс', 'topic': 'ЕГЭ сборный', 'kit': 'ЕГЭ сборный · №1, 2 (20.10)', 'tasks_count': 5}
        works = catalog.build_works([
            dict(common, id=2, name='11 кл · ЕГЭ сборный · №1, 2 · В2', created_at='2026-10-20 05:00:00'),
            dict(common, id=1, name='11 кл · ЕГЭ сборный · №1, 2 · В1', created_at='2026-10-20 05:00:00'),
            {'id': 3, 'name': '7 кл · 7Г · урок 06.02', 'grade': '7 класс', 'created_at': '2026-02-06 05:00:00', 'tasks_count': 7},
        ])
        self.assertEqual([w['list_title'] for w in works], ['ЕГЭ сборный · №1, 2', '7Г · урок 06.02'])
        kit = works[0]
        self.assertTrue(kit['is_kit'] and kit['launchable'] and kit['topic_in_title'])
        self.assertEqual([v['short'] for v in kit['variants']], ['В1', 'В2'])
        self.assertEqual(catalog.grade_short('11 класс'), '11')


class CatalogPageTests(unittest.TestCase):
    def setUp(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])
        self.client, self.token = teacher()

    def tearDown(self):
        for item in TestSession.get_all():
            if item['status'] == 'active':
                TestSession.close(item['id'])

    def upload(self, *files):
        data = {'csrf_token': self.token, 'file': [(f, name) for name, f in files]}
        return self.client.post('/variants/import-localshot', data=data, content_type='multipart/form-data',
                                follow_redirects=True)

    def test_several_files_with_and_without_passport(self):
        page = self.upload(
            ('Логика (25 мин).variant', variant_file('Логика (25 мин)', passport={'grade': 8, 'topic': 'Логика', 'kit': 'Логика 25', 'minutes': 25})),
            ('Логика (25 мин) - вариант 2.variant', variant_file('Логика (25 мин) - вариант 2', passport={'grade': 8, 'topic': 'Логика', 'kit': 'Логика 25', 'minutes': 25})),
            ('Кодирование 9 класс.variant', variant_file('Кодирование 9 класс')),
            ('битый.variant', io.BytesIO(b'not a zip')),
        ).get_data(as_text=True)
        self.assertIn('Загружено вариантов: 3', page)
        self.assertIn('битый.variant', page)
        first = by_name('Логика (25 мин)')
        self.assertEqual((first['grade'], first['topic'], first['kit'], first['minutes']), ('8 класс', 'Логика', 'Логика 25', 25))
        guessed = by_name('Кодирование 9 класс')
        self.assertEqual((guessed['grade'], guessed['topic']), ('9 класс', 'Кодирование информации'))
        self.assertIn('Запустить комплект', self.client.get('/variants').get_data(as_text=True))

    def test_first_variant_uploaded_before_others_joins_kit(self):
        self.upload(('Фано.variant', variant_file('Фано (10 мин)')))
        self.assertIsNone(by_name('Фано (10 мин)')['kit'])
        self.upload(('Фано 2.variant', variant_file('Фано (10 мин) - вариант 2')))
        self.assertEqual(by_name('Фано (10 мин)')['kit'], 'Фано (10 мин)')

    def test_move_by_drag_and_by_form(self):
        self.upload(('Перенос.variant', variant_file('Перенос')))
        vid = by_name('Перенос')['id']
        r = self.client.post(f'/variants/{vid}/catalog', json={'grade': '10 класс', 'topic': 'Графы', 'kit': ''},
                             headers={'X-CSRFToken': self.token})
        self.assertTrue(r.get_json()['ok'])
        v = by_name('Перенос')
        self.assertEqual((v['grade'], v['topic'], v['kit']), ('10 класс', 'Графы', None))
        self.client.post(f'/variants/{vid}/catalog', data={'csrf_token': self.token, 'grade': '11', 'topic': 'Графы',
                                                           'kit': 'Графы 1', 'minutes': '30'})
        v = by_name('Перенос')
        self.assertEqual((v['grade'], v['kit'], v['minutes']), ('11 класс', 'Графы 1', 30))

    def test_students_cannot_change_catalog(self):
        vid = Variant.get_all()[0]['id'] if Variant.get_all() else Variant.create('x', 'uploaded')
        pupil = server.app.test_client()
        self.assertEqual(pupil.post(f'/variants/{vid}/catalog', json={'grade': '1'},
                                    environ_base={'REMOTE_ADDR': '192.168.0.9'}).status_code, 403)

    def test_launch_kit_preselects_variants_and_time(self):
        passport = {'grade': 9, 'topic': 'Сети', 'kit': 'IP-адреса', 'minutes': 35}
        self.upload(('IP 1.variant', variant_file('IP 1', passport=passport)),
                    ('IP 2.variant', variant_file('IP 2', passport=passport)))
        page = self.client.get('/sessions/new?kit=IP-адреса&kit_grade=9 класс&kit_topic=Сети').get_data(as_text=True)
        self.assertIn('Комплект «IP-адреса»', page)
        self.assertIn('value="35"', page)
        for name in ('IP 1', 'IP 2'):
            vid = by_name(name)['id']
            self.assertIn(f'value="{vid}" data-count="2" checked', page)
        self.assertRegex(page, r'value="multi_choice"\s+checked')

    def test_average_result(self):
        self.upload(('Средний.variant', variant_file('Средний', answers=('1', '2'))))
        v = by_name('Средний')
        sid = TestSession.create(v['id'], 0, 20, None, 1, 1, 0, 0)
        tasks = Variant.get_tasks(v['id'])
        a = Student.create(sid, 'А', 'А', v['id'])
        b = Student.create(sid, 'Б', 'Б', v['id'])
        Answer.save(a, tasks[0]['id'], answer_1='1'); Answer.save(a, tasks[1]['id'], answer_1='2')  # 100%
        Answer.save(b, tasks[0]['id'], answer_1='1')                                                 # 50%
        TestSession.close(sid)
        self.assertEqual(Variant.results_stats()[v['id']], {'runs': 2, 'percent': 75})
        self.assertIn('75%', self.client.get(f'/variants?variant_id={v["id"]}').get_data(as_text=True))

    def test_old_variants_are_sorted_once(self):
        vid = Variant.create('Алфавитный подход (7 класс, 20 минут) - вариант 5', 'uploaded')
        set_setting('catalog_autofill_done', '0')
        autofill_catalog_once()
        v = Variant.get_by_id(vid)
        self.assertEqual((v['grade'], v['topic']), ('7 класс', 'Алфавитный подход'))
        Variant.set_catalog(vid, grade='8 класс')
        autofill_catalog_once()  # второй раз ничего не трогает
        self.assertEqual(Variant.get_by_id(vid)['grade'], '8 класс')


if __name__ == '__main__':
    unittest.main()
