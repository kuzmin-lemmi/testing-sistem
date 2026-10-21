"""Брандмауэр: кнопка «Открыть доступ ученикам» и проверка, открыт ли доступ (без настоящих netsh/PowerShell).

Запуск из корня проекта:  python -m unittest discover -s tests -v
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import activity  # noqa: E402


def completed(code=0, out=b''):
    return subprocess.CompletedProcess([], code, out, b'')


@unittest.skipUnless(sys.platform == 'win32', 'только Windows')
class FirewallTests(unittest.TestCase):
    def setUp(self):
        activity._port_check.update({'open': None, 'at': 0.0, 'running': False})

    def test_open_passes_rule_name_in_double_quotes(self):
        with mock.patch('activity.subprocess.run', return_value=completed()) as run:
            activity.open_firewall()
        command = run.call_args_list[0].args[0][-1]
        self.assertIn(f"'name=\"{activity.FIREWALL_RULE}\"'", command)  # одинарные кавычки netsh не понимает
        self.assertNotIn(f"name='{activity.FIREWALL_RULE}'", command)
        self.assertIn('-Verb RunAs', command)

    def test_own_rule_is_enough(self):
        with mock.patch('activity.subprocess.run', return_value=completed(0)):
            self.assertEqual(activity.firewall_status(), 'ok')

    def test_other_rule_for_the_port_counts(self):
        # своего правила нет, но есть другое (например, созданное вручную): проверка по порту идёт в фоне
        def fake(args, **kwargs):
            return completed(1) if args[0] == 'netsh' else completed(0, b'open\r\n')
        with mock.patch('activity.subprocess.run', side_effect=fake), \
                mock.patch('activity.threading.Thread') as thread:
            self.assertEqual(activity.firewall_status(), 'unknown')  # пока проверяем — не пугаем учителя
            thread.call_args.kwargs['target']()
            self.assertEqual(activity.firewall_status(), 'ok')

    def test_no_rule_at_all(self):
        def fake(args, **kwargs):
            return completed(1) if args[0] == 'netsh' else completed(0, b'closed\r\n')
        with mock.patch('activity.subprocess.run', side_effect=fake), \
                mock.patch('activity.threading.Thread') as thread:
            activity.firewall_status()
            thread.call_args.kwargs['target']()
            self.assertEqual(activity.firewall_status(), 'missing')


if __name__ == '__main__':
    unittest.main()
