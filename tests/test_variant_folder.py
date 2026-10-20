"""Папка с вариантами (банк D:\\Варианты): настройка, поиск новых файлов, загрузка только новых.

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_localshot_import import make_variant_file  # noqa: E402  (заодно задаёт временную папку данных)

import server  # noqa: E402
import variant_folder  # noqa: E402
from models import Variant, get_setting, set_setting  # noqa: E402


class VariantFolderTests(unittest.TestCase):
    def setUp(self):
        self.bank = tempfile.mkdtemp(prefix='bank-')
        self.addCleanup(shutil.rmtree, self.bank, True)
        os.makedirs(os.path.join(self.bank, '2026', '2026-10-21 7 кл · Логика · таблицы'))
        os.makedirs(os.path.join(self.bank, '_исходные'))
        os.makedirs(os.path.join(self.bank, 'кэш'))
        self.write('2026/2026-10-21 7 кл · Логика · таблицы/В1.variant', '7 кл · Логика · таблицы · В1', 'a1a1a1000001')
        self.write('2026/2026-10-21 7 кл · Логика · таблицы/В2.variant', '7 кл · Логика · таблицы · В2', 'a1a1a1000002')
        self.write('_исходные/старый.variant', 'старый вариант', 'a1a1a1000003')   # служебная папка — не трогаем
        self.write('кэш/мусор.variant', 'кэш', 'a1a1a1000004')
        self.old_setting = get_setting(variant_folder.SETTING)
        set_setting(variant_folder.SETTING, self.bank)
        self.addCleanup(set_setting, variant_folder.SETTING, self.old_setting)
        self.client = server.app.test_client()
        self.client.get('/variants')
        with self.client.session_transaction() as session:
            self.token = session['_csrf_token']

    def write(self, rel, name, task_id):
        data = make_variant_file([{'id': task_id, 'answers': ['5']}], name=name,
                                 manifest_extra={'catalog': {'grade': '7', 'topic': 'Логика', 'kit': 'Логика · таблицы (21.10)'}})
        with open(os.path.join(self.bank, rel), 'wb') as f:
            f.write(data)

    def names(self):
        return {v['name'] for v in Variant.get_all()}

    def test_scan_skips_service_folders(self):
        found = [os.path.basename(p) for p in variant_folder.variant_files(self.bank)]
        self.assertEqual(sorted(found), ['В1.variant', 'В2.variant'])

    def test_import_only_new_and_only_once(self):
        page = self.client.get('/variants').get_data(as_text=True)
        self.assertIn('новых: <strong>2</strong>', page)
        self.client.post('/variants/import-folder', data={'csrf_token': self.token}, follow_redirects=True)
        self.assertTrue({'7 кл · Логика · таблицы · В1', '7 кл · Логика · таблицы · В2'} <= self.names())
        before = len(Variant.get_all())
        page = self.client.post('/variants/import-folder', data={'csrf_token': self.token}, follow_redirects=True).get_data(as_text=True)
        self.assertEqual(len(Variant.get_all()), before)            # второй раз ничего не загружается
        self.assertIn('Новых вариантов в папке нет', page)
        self.assertIn('все варианты из папки загружены', page)

    def test_settings_save_and_reject_missing_folder(self):
        page = self.client.get('/settings').get_data(as_text=True)
        self.assertIn('id="variants-dir"', page)
        missing = os.path.join(self.bank, 'нет такой папки')
        page = self.client.post('/settings/variants-dir', data={'csrf_token': self.token, 'variants_dir': missing},
                                follow_redirects=True).get_data(as_text=True)
        self.assertIn('Папка не найдена', page)
        self.assertEqual(get_setting(variant_folder.SETTING), self.bank)
        other = tempfile.mkdtemp(prefix='bank2-')
        self.addCleanup(shutil.rmtree, other, True)
        self.client.post('/settings/variants-dir', data={'csrf_token': self.token, 'variants_dir': other})
        self.assertEqual(get_setting(variant_folder.SETTING), other)

    def test_pick_folder_returns_dialog_choice(self):
        with patch.object(variant_folder, 'pick_folder', return_value=r'E:\Банк'):
            r = self.client.post('/settings/pick-folder', json={'start': ''}, headers={'X-CSRFToken': self.token})
        self.assertEqual(r.get_json()['path'], r'E:\Банк')

    def test_students_cannot_use_folder_routes(self):
        for path in ('/variants/import-folder', '/settings/pick-folder', '/settings/variants-dir', '/variants/open-folder'):
            r = self.client.post(path, data={'csrf_token': self.token}, environ_base={'REMOTE_ADDR': '192.168.1.50'})
            self.assertEqual(r.status_code, 403, path)


if __name__ == '__main__':
    unittest.main()
