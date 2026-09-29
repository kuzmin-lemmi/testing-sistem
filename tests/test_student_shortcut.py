"""Ярлык для компьютеров учеников: «Тестирование.html» и проверка «система запущена».

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import atexit
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

if 'EGE_DATA_DIR' not in os.environ:
    _DATA = tempfile.mkdtemp(prefix='ege-test-')
    os.environ['EGE_DATA_DIR'] = _DATA
    atexit.register(shutil.rmtree, _DATA, True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import server  # noqa: E402
import student_shortcut  # noqa: E402

STUDENT_PC = {'REMOTE_ADDR': '192.168.1.77'}


class AddressTests(unittest.TestCase):
    def test_name_goes_first_then_ip_addresses(self):
        with patch('student_shortcut.socket.gethostname', return_value='KAB-205'), \
                patch('student_shortcut.local_addresses', return_value=['192.168.1.5', '10.8.1.2']):
            self.assertEqual(student_shortcut.student_urls(8080),
                             ['http://KAB-205:8080', 'http://192.168.1.5:8080', 'http://10.8.1.2:8080'])

    def test_loopback_and_link_local_addresses_are_ignored(self):
        infos = [(2, 1, 6, '', (address, 0)) for address in
                 ('127.0.0.1', '169.254.10.4', '192.168.1.5', '192.168.1.5', '10.8.1.2')]
        with patch('student_shortcut.socket.getaddrinfo', return_value=infos), \
                patch('student_shortcut.socket.socket', side_effect=OSError('нет сети')):
            self.assertEqual(student_shortcut.local_addresses(), ['192.168.1.5', '10.8.1.2'])

    def test_without_any_network_address_falls_back_to_localhost(self):
        with patch('student_shortcut.socket.gethostname', return_value=''), \
                patch('student_shortcut.local_addresses', return_value=[]):
            self.assertEqual(student_shortcut.student_urls(8080), ['http://127.0.0.1:8080'])

    def test_teacher_ip_shown_on_screen_works_without_internet(self):
        with patch('server.local_addresses', return_value=['192.168.1.5']):
            self.assertEqual(server.get_local_ip(), '192.168.1.5')


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_alive_answers_students_even_when_no_test_is_running(self):
        response = self.client.get('/test/alive', environ_base=STUDENT_PC)
        self.assertEqual((response.status_code, response.get_data(as_text=True)), (200, 'ok'))
        self.assertIn('no-store', response.headers['Cache-Control'])

    def test_teacher_pages_stay_closed_to_student_computers(self):
        self.assertEqual(self.client.get('/variants', environ_base=STUDENT_PC).status_code, 403)
        self.assertEqual(self.client.get('/shortcut/download', environ_base=STUDENT_PC).status_code, 403)

    def test_shortcut_download(self):
        urls = ['http://KAB-205:8080', 'http://192.168.1.5:8080']
        with patch('server.student_urls', return_value=urls):
            response = self.client.get('/shortcut/download')
        self.assertEqual(response.status_code, 200)
        disposition = response.headers['Content-Disposition']
        self.assertTrue(disposition.startswith('attachment'))
        self.assertIn("filename*=UTF-8''%D0%A2%D0%B5%D1%81%D1%82%D0%B8%D1%80%D0%BE%D0%B2%D0%B0%D0%BD%D0%B8%D0%B5.html", disposition)
        page = response.get_data(as_text=True)
        self.assertIn('const ADDRESSES = ["http://KAB-205:8080", "http://192.168.1.5:8080"];', page)
        self.assertIn("'/test/alive'", page)
        self.assertIn("'/test'", page)
        self.assertIn('Тестирование ещё не началось', page)
        # файл самодостаточен: не тянет ничего из системы (её ведь может ещё не быть в сети)
        self.assertNotIn('/static/', page)
        self.assertNotIn('https://', page)
        response.close()

    def test_addresses_cannot_break_out_of_the_script(self):
        with patch('server.student_urls', return_value=['http://</script><script>alert(1)</script>:8080']):
            page = self.client.get('/shortcut/download').get_data(as_text=True)
        self.assertNotIn('</script><script>alert(1)', page)

    def test_teacher_home_page_offers_the_shortcut(self):
        with patch('server.student_urls', return_value=['http://KAB-205:8080']):
            page = self.client.get('/').get_data(as_text=True)
        self.assertIn('/shortcut/download', page)
        self.assertIn('http://KAB-205:8080', page)


if __name__ == '__main__':
    unittest.main()
