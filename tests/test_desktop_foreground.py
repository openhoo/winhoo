"""A foreground sample belongs to one request on one unlocked console."""
import json
import subprocess
import unittest
import uuid
from unittest.mock import patch

import winhoo_macos as desktop


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        self.state = {'uuid': str(uuid.uuid4()), 'account': 'TestUser'}
        self.request = uuid.uuid4()
        self.sample = {
            'owner': self.state['uuid'], 'request': self.request.hex,
            'account': 'TESTUSER', 'computer': 'TEST',
            'consoleSession': {'account': r'TEST\TestUser', 'id': 1, 'connection': 'active', 'lock': 'unlocked'},
            'console': r'TEST\TestUser', 'session': 1, 'pid': 37, 'process': 'explorer',
        }

    def observe(self, sample, expected=None):
        reply = subprocess.CompletedProcess([], 0, json.dumps(sample), '')
        with patch.object(desktop, 'guest', return_value=reply), patch.object(desktop.uuid, 'uuid4', return_value=self.request):
            return desktop.foreground(None, self.state, expected)

    def test_rejects_explorer_when_the_browser_was_expected(self):
        self.assertEqual(self.observe(self.sample, 37), self.sample)
        with self.assertRaisesRegex(ValueError, 'explorer.*37.*42'):
            self.observe(self.sample, 42)

    def test_rejects_stale_foreign_locked_or_noninteractive_sample(self):
        for change in (
            {'request': uuid.uuid4().hex}, {'owner': str(uuid.uuid4())},
            {'account': 'Other'}, {'session': 0}, {'session': 2}, {'pid': 0},
            {'consoleSession': {**self.sample['consoleSession'], 'lock': 'locked'}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.observe({**self.sample, **change})

    def test_transport_timeout_does_not_return_an_earlier_window(self):
        with patch.object(desktop, 'guest', side_effect=subprocess.TimeoutExpired('ssh', 45)) as probe:
            with self.assertRaisesRegex(ValueError, 'no window was confirmed'):
                desktop.foreground(None, self.state)
        probe.assert_called_once()
